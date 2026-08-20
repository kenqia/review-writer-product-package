#!/usr/bin/env python3
"""Assemble a grounded evidence candidate from immutable atoms and semantic decisions."""

from __future__ import annotations

import argparse
import collections
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from evidence_atom_core import (
    HIGH_RISK_CATEGORIES,
    RENDERER_CONTRACT,
    EvidenceAtomCoreError,
    canonical_json_sha256,
    canonical_sealed_job_id,
    canonicalize_text,
    packet_path,
    render_pdf_page,
    sha256_file,
    verify_job_source_layers,
)


class AssemblyError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_schema(instance: Any, schema: dict, code: str) -> None:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(instance),
        key=lambda error: tuple(str(part) for part in error.path),
    )
    if errors:
        raise AssemblyError(code, errors[0].message)


def parse_source_pdfs(values: list[str]) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    for value in values:
        source_id, separator, raw_path = value.partition("=")
        if not separator or not source_id or not raw_path or source_id in sources:
            raise AssemblyError(
                "SOURCE_PDF_ARGUMENT_INVALID",
                "--source-pdf must be a unique SOURCE_ID=/local/file.pdf mapping",
            )
        sources[source_id] = Path(raw_path)
    return sources


def validate_visual_provenance(
    atom: dict[str, Any],
    job: dict[str, Any],
    packet_root: Path,
    source: dict[str, Any],
    source_pdfs: dict[str, Path],
    renderer: Path | None,
) -> None:
    declarations = [
        item
        for item in job.get("visual_crops", [])
        if isinstance(item, dict)
        and item.get("source_id") == atom.get("source_id")
        and item.get("page") == atom.get("page")
        and item.get("manifest_path") == atom.get("crop_manifest_path")
    ]
    if len(declarations) != 1:
        raise AssemblyError(
            "VISUAL_MANIFEST_JOB_MISMATCH",
            f"visual atom is not uniquely bound by the sealed job: {atom.get('atom_id')!r}",
        )
    try:
        manifest_path = packet_path(packet_root, atom["crop_manifest_path"])
    except (KeyError, OSError, ValueError) as exc:
        raise AssemblyError("VISUAL_MANIFEST_INVALID", str(exc)) from exc
    if not manifest_path.is_file():
        raise AssemblyError("VISUAL_MANIFEST_INVALID", "bound crop manifest is missing")
    observed_manifest_hash = sha256_file(manifest_path)
    if (
        observed_manifest_hash != declarations[0].get("manifest_sha256")
        or observed_manifest_hash != atom.get("crop_manifest_sha256")
    ):
        raise AssemblyError(
            "VISUAL_MANIFEST_JOB_MISMATCH",
            "crop manifest bytes do not match the sealed job and atom",
        )
    try:
        manifest = load_json(manifest_path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AssemblyError("VISUAL_MANIFEST_INVALID", str(exc)) from exc
    matching_fields = (
        manifest.get("schema_version") == "evidence-page-crop-manifest.v1"
        and manifest.get("source_id") == atom.get("source_id")
        and manifest.get("page") == atom.get("page")
        and manifest.get("source_binary_sha256") == source.get("source_binary_sha256")
        and manifest.get("source_binary_sha256") == atom.get("source_binary_sha256")
        and manifest.get("renderer_contract") == RENDERER_CONTRACT
        and manifest.get("renderer_contract") == atom.get("renderer_contract")
        and manifest.get("renderer_sha256") == atom.get("renderer_sha256")
        and manifest.get("asset_path") == atom.get("asset_path")
        and manifest.get("asset_sha256") == atom.get("asset_sha256")
    )
    if not matching_fields:
        raise AssemblyError(
            "VISUAL_MANIFEST_JOB_MISMATCH",
            "crop manifest provenance fields do not match the sealed job and atom",
        )
    try:
        asset = packet_path(packet_root, atom["asset_path"])
    except (KeyError, OSError, ValueError) as exc:
        raise AssemblyError("VISUAL_ASSET_INVALID", str(exc)) from exc
    if not asset.is_file() or sha256_file(asset) != atom.get("asset_sha256"):
        raise AssemblyError("VISUAL_ASSET_HASH_MISMATCH", "packet crop bytes drifted")

    source_pdf = source_pdfs.get(atom["source_id"])
    if source_pdf is None or not source_pdf.is_file():
        raise AssemblyError(
            "SOURCE_PDF_REQUIRED",
            f"local source PDF mapping is required for {atom['source_id']}",
        )
    if sha256_file(source_pdf) != source.get("source_binary_sha256"):
        raise AssemblyError(
            "SOURCE_PDF_HASH_MISMATCH",
            f"local source PDF hash does not match {atom['source_id']}",
        )
    if renderer is None or not renderer.is_file():
        raise AssemblyError("RENDERER_REQUIRED", "a local renderer is required for visual audit")
    if sha256_file(renderer) != manifest.get("renderer_sha256"):
        raise AssemblyError("RENDERER_HASH_MISMATCH", "renderer bytes do not match crop manifest")
    with tempfile.TemporaryDirectory() as temp_dir:
        rerendered = Path(temp_dir) / "page.png"
        try:
            render_pdf_page(source_pdf, atom["page"], renderer, rerendered)
        except EvidenceAtomCoreError as exc:
            raise AssemblyError(exc.code, str(exc)) from exc
        if sha256_file(rerendered) != atom.get("asset_sha256"):
            raise AssemblyError(
                "VISUAL_RERENDER_HASH_MISMATCH",
                "local PDF rerender does not match the sealed crop asset",
            )


def validate_catalog(
    job: dict,
    job_path: Path,
    catalog: dict,
    packet_root: Path,
    source_pdfs: dict[str, Path],
    renderer: Path | None,
) -> dict[str, dict]:
    if catalog.get("job_id") != job.get("job_id"):
        raise AssemblyError("CATALOG_JOB_MISMATCH", "catalog job_id does not match job")
    if catalog.get("study_id") != (job.get("study") or {}).get("study_id"):
        raise AssemblyError("CATALOG_STUDY_MISMATCH", "catalog study_id does not match job")
    if catalog.get("job_sha256") != sha256_file(job_path):
        raise AssemblyError("CATALOG_JOB_HASH_MISMATCH", "catalog job SHA-256 does not match")
    try:
        sources = verify_job_source_layers(job, packet_root)
    except EvidenceAtomCoreError as exc:
        raise AssemblyError(exc.code, str(exc)) from exc

    atoms: dict[str, dict] = {}
    for atom in catalog.get("atoms", []):
        atom_id = atom.get("atom_id")
        if atom_id in atoms:
            raise AssemblyError("DUPLICATE_ATOM_ID", f"duplicate catalog atom_id: {atom_id!r}")
        payload = {key: value for key, value in atom.items() if key != "atom_sha256"}
        if atom.get("atom_sha256") != canonical_json_sha256(payload):
            raise AssemblyError("ATOM_HASH_MISMATCH", f"atom hash drift: {atom_id!r}")
        source_id = atom.get("source_id")
        if source_id not in sources:
            raise AssemblyError("ATOM_SOURCE_UNKNOWN", f"unknown atom source_id: {source_id!r}")
        source, pages = sources[source_id]
        page = atom.get("page")
        if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= len(pages):
            raise AssemblyError("ATOM_PAGE_OUT_OF_RANGE", f"atom page is invalid: {atom_id!r}")
        if atom.get("evidence_mode") == "TEXT_QUOTE":
            raw_span = atom.get("raw_source_span")
            if not isinstance(raw_span, str) or not raw_span or raw_span not in pages[page - 1]:
                raise AssemblyError(
                    "ATOM_TEXT_NOT_CONTIGUOUS_ON_PAGE",
                    f"atom raw span is not continuous on its bound page: {atom_id!r}",
                )
            if atom.get("canonical_span") != canonicalize_text(raw_span):
                raise AssemblyError(
                    "ATOM_CANONICAL_MISMATCH",
                    f"atom canonical span does not match the canonical helper: {atom_id!r}",
                )
        elif atom.get("evidence_mode") == "FIGURE_TABLE_IMAGE":
            validate_visual_provenance(
                atom,
                job,
                packet_root,
                source,
                source_pdfs,
                renderer,
            )
        atoms[atom_id] = atom
    catalog_payload = {key: value for key, value in catalog.items() if key != "catalog_sha256"}
    if catalog.get("catalog_sha256") != canonical_json_sha256(catalog_payload):
        raise AssemblyError("CATALOG_HASH_MISMATCH", "catalog payload hash does not match")
    return atoms


def evidence_ref(atom: dict, summary: str, visual_values: dict[str, list[str]]) -> dict:
    if atom["evidence_mode"] == "TEXT_QUOTE":
        return {
            "evidence_mode": "TEXT_QUOTE",
            "source_id": atom["source_id"],
            "page": atom["page"],
            "section_or_item": f"Evidence atom {atom['atom_id']}",
            "exact_quote": atom["raw_source_span"],
            "evidence_summary": summary,
            "depiction_locator": None,
            "transcribed_values": [],
            "r3_flags": [],
        }
    return {
        "evidence_mode": "FIGURE_TABLE_IMAGE",
        "source_id": atom["source_id"],
        "page": atom["page"],
        "section_or_item": f"Evidence atom {atom['atom_id']}",
        "exact_quote": None,
        "evidence_summary": summary,
        "depiction_locator": atom["depiction_locator"],
        "transcribed_values": visual_values[atom["atom_id"]],
        "r3_flags": ["R3_SOURCE_DEPICTION_REQUIRED"],
    }


def semantic_target_contract(job: dict[str, Any]) -> tuple[set[str], set[str]]:
    contract = job.get("semantic_target_contract")
    if contract is None:
        if job.get("schema_version") == "sealed-evidence-extraction-job.v2":
            raise AssemblyError(
                "SEMANTIC_TARGET_CONTRACT_INVALID",
                "sealed v2 jobs require a semantic target contract",
            )
        return {"ELIGIBILITY", "REACTION_UNIT", "CLAIM"}, set()
    if not isinstance(contract, dict):
        raise AssemblyError("SEMANTIC_TARGET_CONTRACT_INVALID", "target contract must be an object")
    allowed = contract.get("allowed_target_kinds")
    denied = contract.get("denied_claim_ids")
    if (
        contract.get("policy") != "ALLOW_EXCEPT_DECLARED_SI_DEPENDENT_CLAIMS"
        or not isinstance(allowed, list)
        or not allowed
        or any(kind not in {"ELIGIBILITY", "REACTION_UNIT", "CLAIM"} for kind in allowed)
        or len(allowed) != len(set(allowed))
        or not isinstance(denied, list)
        or any(not isinstance(claim_id, str) or not claim_id for claim_id in denied)
        or len(denied) != len(set(denied))
    ):
        raise AssemblyError(
            "SEMANTIC_TARGET_CONTRACT_INVALID",
            "sealed semantic target contract is malformed",
        )
    return set(allowed), set(denied)


def validate_sealed_job_binding(job: dict[str, Any]) -> tuple[set[str], set[str]]:
    allowed_target_kinds, denied_claim_ids = semantic_target_contract(job)
    if job.get("schema_version") == "sealed-evidence-extraction-job.v2":
        try:
            expected_job_id = canonical_sealed_job_id(job)
        except EvidenceAtomCoreError as exc:
            raise AssemblyError("JOB_BINDING_INVALID", str(exc)) from exc
        if job.get("job_id") != expected_job_id:
            raise AssemblyError(
                "JOB_BINDING_INVALID",
                "sealed job_id does not match its current source and target bindings",
            )
    return allowed_target_kinds, denied_claim_ids


def assemble(job: dict, catalog: dict, semantic: dict, atoms: dict[str, dict]) -> dict:
    allowed_target_kinds, denied_claim_ids = validate_sealed_job_binding(job)
    if semantic.get("job_id") != job.get("job_id") or semantic.get("study_id") != (
        job.get("study") or {}
    ).get("study_id"):
        raise AssemblyError("SEMANTIC_JOB_MISMATCH", "semantic job or study identity does not match")

    consumed: set[str] = set()
    target_ids: set[str] = set()
    eligibility_decisions = []
    reaction_units = []
    claims = []
    review_items = []
    source_counts: collections.Counter[str] = collections.Counter()

    for decision in semantic["decisions"]:
        target_kind = decision["target_kind"]
        semantic_target_id = decision["target_id"]
        target_id = (
            f"{job['target_namespace']}:{semantic_target_id}"
            if target_kind != "ELIGIBILITY" and "target_namespace" in job
            else semantic_target_id
        )
        if target_kind not in allowed_target_kinds:
            raise AssemblyError(
                "SEMANTIC_TARGET_KIND_DENIED",
                f"semantic target kind is not allowed: {target_kind!r}",
            )
        if target_kind == "CLAIM" and (
            semantic_target_id in denied_claim_ids or target_id in denied_claim_ids
        ):
            raise AssemblyError(
                "BLOCKED_CLAIM_SELECTED",
                f"semantic decision selected a claim denied by source coverage: {semantic_target_id!r}",
            )
        if target_kind != "ELIGIBILITY":
            if target_id in target_ids:
                raise AssemblyError(
                    "DUPLICATE_TARGET_ID",
                    f"duplicate semantic target_id: {semantic_target_id!r}",
                )
            target_ids.add(target_id)
        selected_atoms = []
        for atom_id in decision["atom_ids"]:
            if atom_id not in atoms:
                raise AssemblyError("UNKNOWN_ATOM_ID", f"semantic decision references {atom_id!r}")
            if atom_id in consumed:
                raise AssemblyError("DUPLICATE_ATOM_CONSUMPTION", f"atom consumed twice: {atom_id!r}")
            consumed.add(atom_id)
            selected_atoms.append(atoms[atom_id])
            source_counts[atoms[atom_id]["source_id"]] += 1

        visual_atoms = [
            atom for atom in selected_atoms if atom["evidence_mode"] == "FIGURE_TABLE_IMAGE"
        ]
        value_entries = decision.get("visual_transcribed_values", [])
        visual_values: dict[str, list[str]] = {}
        for entry in value_entries:
            atom_id = entry["atom_id"]
            if atom_id in visual_values:
                raise AssemblyError("VISUAL_VALUES_INVALID", f"duplicate visual values: {atom_id!r}")
            visual_values[atom_id] = entry["values"]
        if set(visual_values) != {atom["atom_id"] for atom in visual_atoms}:
            raise AssemblyError(
                "VISUAL_VALUES_INCOMPLETE",
                "visual_transcribed_values must map every and only visual atom in the decision",
            )

        categories = set(decision.get("semantic_risk_categories", []))
        for atom in selected_atoms:
            categories.update(atom.get("r3_floor_categories", []))
        if visual_atoms:
            categories.add("FIGURE_TABLE_CHEMISTRY")
        if target_kind == "ELIGIBILITY" and (categories or visual_atoms):
            raise AssemblyError(
                "ELIGIBILITY_HIGH_RISK_FORBIDDEN",
                "eligibility may consume text atoms without high-risk categories only",
            )
        refs = [evidence_ref(atom, decision["evidence_summary"], visual_values) for atom in selected_atoms]
        risk_level = "R3" if categories & HIGH_RISK_CATEGORIES else "R1"
        sorted_categories = sorted(categories)

        if target_kind == "ELIGIBILITY":
            eligibility_decisions.append((decision, refs))
        elif target_kind == "REACTION_UNIT":
            reaction_units.append(
                {
                    "reaction_unit_id": target_id,
                    "statement": decision["statement"],
                    "risk_level": risk_level,
                    "risk_categories": sorted_categories,
                    "evidence_refs": refs,
                }
            )
        elif target_kind == "CLAIM":
            claims.append(
                {
                    "claim_id": target_id,
                    "claim_text": decision["statement"],
                    "risk_level": risk_level,
                    "risk_categories": sorted_categories,
                    "evidence_refs": refs,
                }
            )
        if target_kind != "ELIGIBILITY" and risk_level == "R3":
            review_items.append(
                {
                    "target_id": target_id,
                    "risk_categories": sorted_categories,
                    "action_required": "Inspect the atom-bound source evidence before scientific promotion.",
                }
            )

    if len(eligibility_decisions) != 1:
        raise AssemblyError("ELIGIBILITY_DECISION_INVALID", "exactly one eligibility decision is required")
    if not reaction_units:
        raise AssemblyError("REACTION_UNIT_REQUIRED", "at least one reaction-unit decision is required")
    eligibility_decision, eligibility_refs = eligibility_decisions[0]

    source_coverage = {}
    for source in job.get("source_files", []):
        source_id = source["source_id"]
        count = source_counts[source_id]
        source_coverage[source_id] = {
            "status": "READ_AND_USED" if count else "READ_NO_RELEVANT_EVIDENCE",
            "evidence_ref_count": count,
            "note": (
                "Coverage derived from consumed evidence atom IDs."
                if count
                else "No semantic decision consumed an evidence atom from this source."
            ),
        }

    study = job.get("study") or {}
    return {
        "schema_version": "evidence-candidate.v2",
        "job_id": job["job_id"],
        "study_id": study["study_id"],
        "doi": study.get("doi"),
        "eligibility": {
            "status": semantic["eligibility_status"],
            "rationale": eligibility_decision["statement"],
            "evidence_refs": eligibility_refs,
        },
        "study_card": {},
        "anchor_reaction_unit_id": reaction_units[0]["reaction_unit_id"],
        "reaction_units": reaction_units,
        "claims": claims,
        "conflicts": [],
        "r3_review_items": review_items,
        "source_coverage": source_coverage,
        "extraction_limitations": [],
        "self_check": {"status": "DETERMINISTIC_ATOM_ASSEMBLY"},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble evidence-candidate.v2 from sealed atoms.")
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--packet-root", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--semantic", required=True, type=Path)
    parser.add_argument("--catalog-schema", required=True, type=Path)
    parser.add_argument("--semantic-schema", required=True, type=Path)
    parser.add_argument("--candidate-schema", required=True, type=Path)
    parser.add_argument(
        "--source-pdf",
        action="append",
        default=[],
        metavar="SOURCE_ID=/local/file.pdf",
    )
    parser.add_argument("--renderer", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        job = load_json(args.job)
        validate_sealed_job_binding(job)
        catalog = load_json(args.catalog)
        semantic = load_json(args.semantic)
        validate_schema(catalog, load_json(args.catalog_schema), "CATALOG_SCHEMA_INVALID")
        validate_schema(semantic, load_json(args.semantic_schema), "SEMANTIC_SCHEMA_INVALID")
        atoms = validate_catalog(
            job,
            args.job,
            catalog,
            args.packet_root.resolve(),
            parse_source_pdfs(args.source_pdf),
            args.renderer,
        )
        candidate = assemble(job, catalog, semantic, atoms)
        validate_schema(candidate, load_json(args.candidate_schema), "CANDIDATE_SCHEMA_INVALID")
    except AssemblyError as exc:
        sys.stderr.write(f"{exc.code}: {exc}\n")
        return 1
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
