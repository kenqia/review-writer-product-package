#!/usr/bin/env python3
"""Build page-local evidence atoms from explicit sealed selections."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

try:
    from .evidence_atom_core import (
        HIGH_RISK_CATEGORIES,
        RENDERER_CONTRACT,
        EvidenceAtomCoreError,
        canonical_json_sha256,
        canonicalize_text,
        packet_path,
        sha256_file,
        verify_job_source_layers,
    )
except ImportError:  # Direct-script fallback.
    from evidence_atom_core import (
        HIGH_RISK_CATEGORIES,
        RENDERER_CONTRACT,
        EvidenceAtomCoreError,
        canonical_json_sha256,
        canonicalize_text,
        packet_path,
        sha256_file,
        verify_job_source_layers,
    )


IMAGE_SUFFIX = ".png"


class AtomBuildError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def r3_floors(selected: dict[str, Any]) -> set[str]:
    floors = selected.get("r3_floor_categories", [])
    if (
        not isinstance(floors, list)
        or len(floors) != len(set(floors))
        or any(value not in HIGH_RISK_CATEGORIES for value in floors)
    ):
        raise AtomBuildError("SELECTION_SCHEMA_INVALID", "invalid r3_floor_categories")
    return set(floors)


def load_bound_crop_manifest(
    job: dict[str, Any],
    packet_root: Path,
    source: dict[str, Any],
    selected: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    manifest_path = selected.get("crop_manifest_path")
    if not isinstance(manifest_path, str):
        raise AtomBuildError("VISUAL_MANIFEST_INVALID", "crop_manifest_path is required")
    declarations = [
        item
        for item in job.get("visual_crops", [])
        if isinstance(item, dict)
        and item.get("source_id") == selected.get("source_id")
        and item.get("page") == selected.get("page")
        and item.get("manifest_path") == manifest_path
    ]
    if len(declarations) != 1:
        raise AtomBuildError(
            "VISUAL_MANIFEST_JOB_MISMATCH",
            "crop manifest is not uniquely bound by the sealed job",
        )
    try:
        manifest_file = packet_path(packet_root, manifest_path)
    except (OSError, ValueError) as exc:
        raise AtomBuildError("VISUAL_MANIFEST_INVALID", str(exc)) from exc
    if not manifest_file.is_file():
        raise AtomBuildError("VISUAL_MANIFEST_INVALID", "crop manifest is missing")
    observed_manifest_hash = sha256_file(manifest_file)
    if observed_manifest_hash != declarations[0].get("manifest_sha256"):
        raise AtomBuildError(
            "VISUAL_MANIFEST_JOB_MISMATCH",
            "crop manifest bytes do not match the sealed job",
        )
    try:
        manifest = load_json(manifest_file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AtomBuildError("VISUAL_MANIFEST_INVALID", str(exc)) from exc
    required_identity = (
        manifest.get("schema_version") == "evidence-page-crop-manifest.v1"
        and manifest.get("source_id") == selected.get("source_id")
        and manifest.get("page") == selected.get("page")
        and manifest.get("source_binary_sha256") == source.get("source_binary_sha256")
        and manifest.get("renderer_contract") == RENDERER_CONTRACT
    )
    if not required_identity:
        raise AtomBuildError(
            "VISUAL_MANIFEST_JOB_MISMATCH",
            "crop manifest identity does not match source, page, or renderer contract",
        )
    return manifest, observed_manifest_hash


def build_catalog(job_path: Path, selection: dict, packet_root: Path, schema: dict) -> dict:
    job_bytes = job_path.read_bytes()
    job = json.loads(job_bytes.decode("utf-8"))
    try:
        sources = verify_job_source_layers(job, packet_root)
    except EvidenceAtomCoreError as exc:
        raise AtomBuildError(exc.code, str(exc)) from exc
    if selection.get("schema_version") != "evidence-atom-selection.v1":
        raise AtomBuildError("SELECTION_SCHEMA_INVALID", "unexpected selection schema_version")
    selections = selection.get("atoms")
    if not isinstance(selections, list) or not selections:
        raise AtomBuildError("SELECTION_SCHEMA_INVALID", "selection requires a nonempty atoms array")

    atoms = []
    seen_ids: set[str] = set()
    for selected in selections:
        if not isinstance(selected, dict):
            raise AtomBuildError("SELECTION_SCHEMA_INVALID", "each selection must be an object")
        atom_id = selected.get("atom_id")
        if not isinstance(atom_id, str) or not atom_id or atom_id in seen_ids:
            raise AtomBuildError("DUPLICATE_ATOM_ID", "selection atom_id is absent or duplicated")
        seen_ids.add(atom_id)
        source_id = selected.get("source_id")
        if source_id not in sources:
            raise AtomBuildError("UNKNOWN_SOURCE_ID", f"unknown source_id: {source_id!r}")
        source, pages = sources[source_id]
        page = selected.get("page")
        if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= len(pages):
            raise AtomBuildError("PAGE_OUT_OF_RANGE", f"page is outside source {source_id}")
        mode = selected.get("evidence_mode")
        atom = {
            "atom_id": atom_id,
            "source_id": source_id,
            "page": page,
            "evidence_mode": mode,
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
        floors = r3_floors(selected)
        if mode == "TEXT_QUOTE":
            raw_span = selected.get("raw_source_span")
            if not isinstance(raw_span, str) or not raw_span or raw_span not in pages[page - 1]:
                raise AtomBuildError(
                    "TEXT_SPAN_NOT_CONTIGUOUS_ON_PAGE",
                    f"atom {atom_id} is not one continuous raw span on the declared page",
                )
            atom["raw_source_span"] = raw_span
            atom["canonical_span"] = canonicalize_text(raw_span)
            atom["r3_floor_categories"] = sorted(floors)
        elif mode == "FIGURE_TABLE_IMAGE":
            if source.get("visual_evidence_allowed") is not True:
                raise AtomBuildError("VISUAL_SOURCE_FORBIDDEN", f"visual evidence is disabled: {source_id}")
            depiction_locator = selected.get("depiction_locator")
            if not isinstance(depiction_locator, str) or not depiction_locator:
                raise AtomBuildError("VISUAL_SELECTION_INVALID", f"visual selection is incomplete: {atom_id}")
            manifest, manifest_hash = load_bound_crop_manifest(
                job,
                packet_root,
                source,
                selected,
            )
            asset_path = manifest.get("asset_path")
            expected_hash = manifest.get("asset_sha256")
            if not isinstance(asset_path, str) or not isinstance(expected_hash, str):
                raise AtomBuildError("VISUAL_MANIFEST_INVALID", f"crop manifest is incomplete: {atom_id}")
            try:
                resolved_asset = packet_path(packet_root, asset_path)
            except (OSError, ValueError) as exc:
                raise AtomBuildError("VISUAL_SELECTION_INVALID", str(exc)) from exc
            if resolved_asset.suffix.casefold() != IMAGE_SUFFIX or not resolved_asset.is_file():
                raise AtomBuildError("VISUAL_SELECTION_INVALID", f"visual crop is not a PNG image: {atom_id}")
            observed_hash = sha256_file(resolved_asset)
            if expected_hash != observed_hash:
                raise AtomBuildError("VISUAL_ASSET_HASH_MISMATCH", f"visual crop drift: {atom_id}")
            atom["asset_path"] = asset_path
            atom["asset_sha256"] = observed_hash
            atom["depiction_locator"] = depiction_locator
            atom["crop_manifest_path"] = selected["crop_manifest_path"]
            atom["crop_manifest_sha256"] = manifest_hash
            atom["source_binary_sha256"] = manifest["source_binary_sha256"]
            atom["renderer_contract"] = manifest["renderer_contract"]
            atom["renderer_sha256"] = manifest["renderer_sha256"]
            atom["r3_floor_categories"] = sorted(floors | {"FIGURE_TABLE_CHEMISTRY"})
        else:
            raise AtomBuildError("SELECTION_SCHEMA_INVALID", f"unsupported evidence_mode: {mode!r}")
        atom["atom_sha256"] = canonical_json_sha256(atom)
        atoms.append(atom)

    catalog = {
        "schema_version": "evidence-atom-catalog.v1",
        "job_id": job.get("job_id"),
        "study_id": (job.get("study") or {}).get("study_id"),
        "job_sha256": sha256_file(job_path),
        "atoms": atoms,
    }
    catalog["catalog_sha256"] = canonical_json_sha256(catalog)
    schema_errors = sorted(
        Draft202012Validator(schema).iter_errors(catalog),
        key=lambda error: tuple(str(part) for part in error.path),
    )
    if schema_errors:
        raise AtomBuildError("CATALOG_SCHEMA_INVALID", schema_errors[0].message)
    return catalog


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a bounded page-local evidence atom catalog.")
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--packet-root", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        catalog = build_catalog(
            args.job,
            load_json(args.selection),
            args.packet_root.resolve(),
            load_json(args.schema),
        )
    except AtomBuildError as exc:
        sys.stderr.write(f"{exc.code}: {exc}\n")
        return 1
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
