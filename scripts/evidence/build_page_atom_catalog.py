#!/usr/bin/env python3
"""Build a deterministic page-local catalog from sealed source bindings.

Reading-order text is the sole source of exact quote atoms. Layout text is
hash-verified by the shared kernel but remains a visual locator layer only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

try:
    from .build_evidence_atoms import AtomBuildError, load_bound_crop_manifest
    from .evidence_atom_core import (
        EvidenceAtomCoreError,
        canonical_json_sha256,
        canonicalize_text,
        packet_path,
        sha256_file,
        verify_job_source_layers,
    )
except ImportError:  # Direct-script fallback.
    from build_evidence_atoms import AtomBuildError, load_bound_crop_manifest
    from evidence_atom_core import (
        EvidenceAtomCoreError,
        canonical_json_sha256,
        canonicalize_text,
        packet_path,
        sha256_file,
        verify_job_source_layers,
    )


PARAGRAPH_SPLIT_RE = re.compile(r"\n[ \t]*\n+")
IMAGE_SUFFIX = ".png"
VISUAL_R3_FLOOR = "FIGURE_TABLE_CHEMISTRY"


class PageCatalogError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atom_base(atom_id: str, source_id: str, page: int, evidence_mode: str) -> dict[str, Any]:
    return {
        "atom_id": atom_id,
        "source_id": source_id,
        "page": page,
        "evidence_mode": evidence_mode,
        "raw_source_span": None,
        "canonical_span": None,
        "asset_path": None,
        "asset_sha256": None,
        "depiction_locator": None,
        "crop_manifest_path": None,
        "crop_manifest_sha256": None,
        "source_binary_sha256": None,
        "renderer_contract": None,
        "renderer_sha256": None,
        "r3_floor_categories": [],
    }


def seal_atom(atom: dict[str, Any]) -> dict[str, Any]:
    atom["atom_sha256"] = canonical_json_sha256(atom)
    return atom


def build_text_atoms(
    sources: dict[str, tuple[dict[str, Any], list[str]]],
) -> list[dict[str, Any]]:
    atoms: list[dict[str, Any]] = []
    for source_id in sorted(sources):
        _, pages = sources[source_id]
        for page_number, page_text in enumerate(pages, start=1):
            span_number = 0
            for raw_span in PARAGRAPH_SPLIT_RE.split(page_text):
                if not raw_span.strip():
                    continue
                span_number += 1
                atom = atom_base(
                    f"{source_id}:p{page_number}:t{span_number}",
                    source_id,
                    page_number,
                    "TEXT_QUOTE",
                )
                atom["raw_source_span"] = raw_span
                atom["canonical_span"] = canonicalize_text(raw_span)
                atoms.append(seal_atom(atom))
    return atoms


def validate_visual_declaration(
    declaration: Any,
    sources: dict[str, tuple[dict[str, Any], list[str]]],
) -> tuple[str, int, str]:
    if not isinstance(declaration, dict):
        raise PageCatalogError("VISUAL_MANIFEST_JOB_MISMATCH", "visual crop must be an object")
    source_id = declaration.get("source_id")
    if not isinstance(source_id, str) or source_id not in sources:
        raise PageCatalogError(
            "VISUAL_SOURCE_UNBOUND",
            f"visual crop source is not bound by the sealed job: {source_id!r}",
        )
    page = declaration.get("page")
    pages = sources[source_id][1]
    if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= len(pages):
        raise PageCatalogError(
            "VISUAL_PAGE_OUT_OF_RANGE",
            f"visual crop page is outside source {source_id}",
        )
    manifest_path = declaration.get("manifest_path")
    if not isinstance(manifest_path, str) or not manifest_path:
        raise PageCatalogError(
            "VISUAL_MANIFEST_INVALID",
            "visual crop manifest_path is required",
        )
    return source_id, page, manifest_path


def build_visual_atoms(
    job: dict[str, Any],
    packet_root: Path,
    sources: dict[str, tuple[dict[str, Any], list[str]]],
) -> list[dict[str, Any]]:
    declarations = job.get("visual_crops", [])
    if not isinstance(declarations, list):
        raise PageCatalogError("VISUAL_MANIFEST_JOB_MISMATCH", "visual_crops must be an array")
    validated = [
        (validate_visual_declaration(declaration, sources), declaration)
        for declaration in declarations
    ]
    validated.sort(key=lambda item: item[0])

    atoms: list[dict[str, Any]] = []
    page_counts: dict[tuple[str, int], int] = {}
    for (source_id, page, manifest_path), declaration in validated:
        source = sources[source_id][0]
        if source.get("visual_evidence_allowed") is not True:
            raise PageCatalogError(
                "VISUAL_SOURCE_FORBIDDEN",
                f"visual evidence is disabled: {source_id}",
            )
        try:
            manifest, manifest_sha256 = load_bound_crop_manifest(
                job,
                packet_root,
                source,
                {
                    "source_id": source_id,
                    "page": page,
                    "crop_manifest_path": manifest_path,
                },
            )
        except AtomBuildError as exc:
            raise PageCatalogError(exc.code, str(exc)) from exc

        asset_path = manifest.get("asset_path")
        expected_asset_sha256 = manifest.get("asset_sha256")
        if not isinstance(asset_path, str) or not isinstance(expected_asset_sha256, str):
            raise PageCatalogError("VISUAL_MANIFEST_INVALID", "crop manifest is incomplete")
        try:
            asset = packet_path(packet_root, asset_path)
        except (OSError, ValueError) as exc:
            raise PageCatalogError("VISUAL_MANIFEST_INVALID", str(exc)) from exc
        if asset.suffix.casefold() != IMAGE_SUFFIX or not asset.is_file():
            raise PageCatalogError(
                "VISUAL_MANIFEST_INVALID",
                f"visual crop is not a PNG image: {asset_path}",
            )
        observed_asset_sha256 = sha256_file(asset)
        if observed_asset_sha256 != expected_asset_sha256:
            raise PageCatalogError(
                "VISUAL_ASSET_HASH_MISMATCH",
                f"visual crop drift: {asset_path}",
            )

        page_key = (source_id, page)
        page_counts[page_key] = page_counts.get(page_key, 0) + 1
        atom = atom_base(
            f"{source_id}:p{page}:v{page_counts[page_key]}",
            source_id,
            page,
            "FIGURE_TABLE_IMAGE",
        )
        atom["asset_path"] = asset_path
        atom["asset_sha256"] = observed_asset_sha256
        atom["depiction_locator"] = f"Page {page} hash-bound full-page crop"
        atom["crop_manifest_path"] = manifest_path
        atom["crop_manifest_sha256"] = manifest_sha256
        atom["source_binary_sha256"] = manifest["source_binary_sha256"]
        atom["renderer_contract"] = manifest["renderer_contract"]
        atom["renderer_sha256"] = manifest["renderer_sha256"]
        atom["r3_floor_categories"] = [VISUAL_R3_FLOOR]
        atoms.append(seal_atom(atom))
    return atoms


def build_page_atom_catalog(job_path: Path, packet_root: Path) -> dict:
    try:
        job = load_json(job_path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PageCatalogError("JOB_INVALID", str(exc)) from exc
    if not isinstance(job, dict):
        raise PageCatalogError("JOB_INVALID", "sealed job must be an object")
    try:
        sources = verify_job_source_layers(job, packet_root.resolve())
    except EvidenceAtomCoreError as exc:
        raise PageCatalogError(exc.code, str(exc)) from exc

    atoms = build_text_atoms(sources)
    atoms.extend(build_visual_atoms(job, packet_root.resolve(), sources))
    catalog = {
        "schema_version": "evidence-atom-catalog.v1",
        "job_id": job.get("job_id"),
        "study_id": (job.get("study") or {}).get("study_id"),
        "job_sha256": sha256_file(job_path),
        "atoms": atoms,
    }
    catalog["catalog_sha256"] = canonical_json_sha256(catalog)
    return catalog


def validate_catalog_schema(catalog: dict, schema: dict[str, Any]) -> None:
    schema_errors = sorted(
        Draft202012Validator(schema).iter_errors(catalog),
        key=lambda error: tuple(str(part) for part in error.path),
    )
    if schema_errors:
        raise PageCatalogError("CATALOG_SCHEMA_INVALID", schema_errors[0].message)


def write_json_atomic(output: Path, payload: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build deterministic page-local atoms from a sealed evidence job."
    )
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--packet-root", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        catalog = build_page_atom_catalog(
            args.job,
            args.packet_root,
        )
        validate_catalog_schema(catalog, load_json(args.schema))
        write_json_atomic(args.output, catalog)
    except PageCatalogError as exc:
        sys.stderr.write(f"{exc.code}: {exc}\n")
        return 1
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
