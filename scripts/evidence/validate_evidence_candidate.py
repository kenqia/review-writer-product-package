#!/usr/bin/env python3
"""Deterministically validate a sealed evidence candidate against page text."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


HIGH_RISK_CATEGORIES = {
    "STRUCTURE",
    "STEREOCHEMISTRY",
    "MECHANISM_CAUSALITY",
    "NEGATIVE_GENERALIZATION",
    "MATERIAL_COMPARISON",
    "FIGURE_TABLE_CHEMISTRY",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def packet_path(packet_root: Path, relative_path: str) -> Path:
    candidate = (packet_root / relative_path).resolve()
    try:
        candidate.relative_to(packet_root.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes packet root: {relative_path}") from exc
    return candidate


def split_pages(path: Path) -> list[str]:
    pages = path.read_text(encoding="utf-8").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def json_path(parts: Any) -> str:
    return "$" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in parts)


def evidence_refs(node: Any, path: tuple[Any, ...] = ()) -> list[tuple[dict, tuple[Any, ...]]]:
    found: list[tuple[dict, tuple[Any, ...]]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = (*path, key)
            if key == "evidence_refs" and isinstance(value, list):
                found.extend(
                    (item, (*child_path, index))
                    for index, item in enumerate(value)
                    if isinstance(item, dict)
                )
            else:
                found.extend(evidence_refs(value, child_path))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(evidence_refs(value, (*path, index)))
    return found


def finding(code: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "path": path, "message": message}


def validate(
    job: dict,
    candidate: dict,
    packet_root: Path,
    schema: dict,
) -> dict:
    findings: list[dict[str, str]] = []
    schema_errors = sorted(
        Draft202012Validator(schema).iter_errors(candidate),
        key=lambda error: json_path(error.path),
    )
    for error in schema_errors:
        findings.append(finding("SCHEMA_INVALID", json_path(error.path), error.message))
    if schema_errors:
        return report(findings, candidate, job, 0)

    if candidate.get("job_id") != job.get("job_id"):
        findings.append(finding("JOB_ID_MISMATCH", "$.job_id", "candidate job_id does not match job"))
    expected_study_id = (job.get("study") or {}).get("study_id")
    if candidate.get("study_id") != expected_study_id:
        findings.append(
            finding("STUDY_ID_MISMATCH", "$.study_id", "candidate study_id does not match job")
        )

    sources: dict[str, dict] = {}
    pages_by_source: dict[str, list[str]] = {}
    layout_pages_by_source: dict[str, list[str]] = {}
    for index, source in enumerate(job.get("source_files", [])):
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or source_id in sources:
            findings.append(
                finding("JOB_SOURCE_INVALID", f"$.source_files[{index}]", "source_id is absent or duplicated")
            )
            continue
        sources[source_id] = source
        reading_path = source.get("reading_order_path")
        if not isinstance(reading_path, str):
            findings.append(
                finding(
                    "READING_ORDER_LAYER_MISSING",
                    f"$.source_files[{index}].reading_order_path",
                    "source requires a packet-relative reading-order layer",
                )
            )
            continue
        try:
            resolved = packet_path(packet_root, reading_path)
            pages_by_source[source_id] = split_pages(resolved)
        except (OSError, UnicodeError, ValueError) as exc:
            findings.append(
                finding(
                    "READING_ORDER_LAYER_UNREADABLE",
                    f"$.source_files[{index}].reading_order_path",
                    str(exc),
                )
            )
            continue
        expected_reading_hash = source.get("reading_order_sha256")
        if not isinstance(expected_reading_hash, str) or sha256_file(resolved) != expected_reading_hash:
            findings.append(
                finding(
                    "SOURCE_HASH_MISMATCH",
                    f"$.source_files[{index}].reading_order_sha256",
                    "reading-order layer does not match its bound SHA-256",
                )
            )

        layout_path = source.get("layout_path")
        if not isinstance(layout_path, str):
            findings.append(
                finding(
                    "LAYOUT_LAYER_MISSING",
                    f"$.source_files[{index}].layout_path",
                    "source requires a packet-relative layout layer",
                )
            )
        else:
            try:
                resolved_layout = packet_path(packet_root, layout_path)
                layout_pages_by_source[source_id] = split_pages(resolved_layout)
            except (OSError, UnicodeError, ValueError) as exc:
                findings.append(
                    finding(
                        "LAYOUT_LAYER_UNREADABLE",
                        f"$.source_files[{index}].layout_path",
                        str(exc),
                    )
                )
            else:
                expected_layout_hash = source.get("layout_sha256")
                if not isinstance(expected_layout_hash, str) or sha256_file(resolved_layout) != expected_layout_hash:
                    findings.append(
                        finding(
                            "SOURCE_HASH_MISMATCH",
                            f"$.source_files[{index}].layout_sha256",
                            "layout layer does not match its bound SHA-256",
                        )
                    )
        declared_pages = source.get("page_count")
        if declared_pages != len(pages_by_source[source_id]):
            findings.append(
                finding(
                    "PAGE_COUNT_MISMATCH",
                    f"$.source_files[{index}].page_count",
                    f"declared {declared_pages!r}; observed {len(pages_by_source[source_id])}",
                )
            )
        if source_id in layout_pages_by_source and declared_pages != len(layout_pages_by_source[source_id]):
            findings.append(
                finding(
                    "PAGE_COUNT_MISMATCH",
                    f"$.source_files[{index}].page_count",
                    f"declared {declared_pages!r}; layout observed {len(layout_pages_by_source[source_id])}",
                )
            )

    refs = evidence_refs(candidate)
    counts: collections.Counter[str] = collections.Counter()
    for ref, ref_path in refs:
        path = json_path(ref_path)
        source_id = ref.get("source_id")
        if source_id not in sources:
            findings.append(finding("SOURCE_ID_UNKNOWN", f"{path}.source_id", "source_id is not bound by job"))
            continue
        counts[source_id] += 1
        mode = ref.get("evidence_mode")
        page = ref.get("page")
        if mode == "LOCATOR_UNRESOLVED":
            findings.append(
                finding("LOCATOR_UNRESOLVED", path, "unresolved evidence cannot pass the grounding gate")
            )
            continue
        pages = pages_by_source.get(source_id)
        if not pages:
            continue
        if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= len(pages):
            findings.append(
                finding(
                    "PAGE_OUT_OF_RANGE",
                    f"{path}.page",
                    f"page must be within 1..{len(pages)}",
                )
            )
            continue
        if mode == "TEXT_QUOTE" and ref.get("exact_quote") not in pages[page - 1]:
            findings.append(
                finding(
                    "EXACT_QUOTE_NOT_FOUND_ON_PAGE",
                    f"{path}.exact_quote",
                    "exact_quote is not a continuous substring of the designated reading-order PDF page",
                )
            )

    targets: dict[str, tuple[str, int, dict]] = {}
    visual_refs_in_mappable_targets: set[int] = set()
    for collection_name, id_field in (("reaction_units", "reaction_unit_id"), ("claims", "claim_id")):
        for index, item in enumerate(candidate.get(collection_name, [])):
            categories = set(item.get("risk_categories", []))
            target_id = item.get(id_field)
            target_path = f"$.{collection_name}[{index}]"
            if target_id in targets:
                findings.append(
                    finding(
                        "TARGET_ID_DUPLICATE",
                        f"{target_path}.{id_field}",
                        "reaction_unit_id and claim_id values must be unique across both collections",
                    )
                )
            else:
                targets[target_id] = (collection_name, index, item)

            direct_visual_refs = [
                ref
                for ref in item.get("evidence_refs", [])
                if isinstance(ref, dict) and ref.get("evidence_mode") == "FIGURE_TABLE_IMAGE"
            ]
            visual_refs_in_mappable_targets.update(id(ref) for ref in direct_visual_refs)
            has_visual_evidence = bool(direct_visual_refs)
            if has_visual_evidence and (
                item.get("risk_level") != "R3" or "FIGURE_TABLE_CHEMISTRY" not in categories
            ):
                findings.append(
                    finding(
                        "VISUAL_PARENT_R3_INVALID",
                        target_path,
                        "visual evidence requires an R3 parent with FIGURE_TABLE_CHEMISTRY",
                    )
                )

            high_risk = bool(categories & HIGH_RISK_CATEGORIES)
            if high_risk and item.get("risk_level") != "R3":
                findings.append(
                    finding(
                        "HIGH_RISK_NOT_R3",
                        f"$.{collection_name}[{index}].risk_level",
                        "high-risk scientific content must be classified R3",
                    )
                )

    for ref, ref_path in refs:
        if (
            ref.get("evidence_mode") == "FIGURE_TABLE_IMAGE"
            and id(ref) not in visual_refs_in_mappable_targets
        ):
            findings.append(
                finding(
                    "VISUAL_EVIDENCE_UNMAPPED_CONTAINER",
                    json_path(ref_path),
                    "visual evidence is allowed only directly on a reaction_unit or claim",
                )
            )

    review_by_target: dict[str, tuple[int, dict]] = {}
    for index, review_item in enumerate(candidate.get("r3_review_items", [])):
        target_id = review_item.get("target_id")
        review_path = f"$.r3_review_items[{index}]"
        if target_id in review_by_target:
            findings.append(
                finding(
                    "R3_REVIEW_TARGET_DUPLICATE",
                    f"{review_path}.target_id",
                    "each target_id may appear in r3_review_items only once",
                )
            )
            continue
        review_by_target[target_id] = (index, review_item)
        if target_id not in targets:
            findings.append(
                finding(
                    "R3_REVIEW_TARGET_UNKNOWN",
                    f"{review_path}.target_id",
                    "r3_review_items target_id must identify one reaction_unit or claim",
                )
            )

    for target_id, (collection_name, index, item) in targets.items():
        categories = set(item.get("risk_categories", []))
        high_risk_categories = categories & HIGH_RISK_CATEGORIES
        has_visual_evidence = any(
            isinstance(ref, dict) and ref.get("evidence_mode") == "FIGURE_TABLE_IMAGE"
            for ref in item.get("evidence_refs", [])
        )
        requires_mapping = bool(
            high_risk_categories or item.get("risk_level") == "R3" or has_visual_evidence
        )
        if requires_mapping and target_id not in review_by_target:
            findings.append(
                finding(
                    "R3_REVIEW_MAPPING_MISSING",
                    f"$.{collection_name}[{index}]",
                    "R3 content must map to one r3_review_items target_id",
                )
            )
            continue
        if target_id in review_by_target:
            review_index, review_item = review_by_target[target_id]
            mapped_categories = set(review_item.get("risk_categories", []))
            if not high_risk_categories.issubset(mapped_categories):
                findings.append(
                    finding(
                        "R3_REVIEW_CATEGORY_MISMATCH",
                        f"$.r3_review_items[{review_index}].risk_categories",
                        "review mapping must cover every high-risk category on its target",
                    )
                )

    coverage = candidate.get("source_coverage", {})
    expected_ids = set(sources)
    actual_ids = set(coverage)
    if actual_ids != expected_ids:
        findings.append(
            finding(
                "SOURCE_COVERAGE_INCOMPLETE",
                "$.source_coverage",
                "coverage keys must exactly match the job source IDs",
            )
        )
    for source_id in sorted(expected_ids & actual_ids):
        entry = coverage[source_id]
        observed = counts[source_id]
        if entry.get("evidence_ref_count") != observed:
            findings.append(
                finding(
                    "SOURCE_COVERAGE_COUNT_MISMATCH",
                    f"$.source_coverage.{source_id}.evidence_ref_count",
                    f"declared {entry.get('evidence_ref_count')!r}; observed {observed}",
                )
            )
        expected_status = "READ_AND_USED" if observed else "READ_NO_RELEVANT_EVIDENCE"
        if entry.get("status") != expected_status:
            findings.append(
                finding(
                    "SOURCE_COVERAGE_STATUS_MISMATCH",
                    f"$.source_coverage.{source_id}.status",
                    f"expected {expected_status} for {observed} evidence refs",
                )
            )

    return report(findings, candidate, job, len(refs))


def report(findings: list[dict[str, str]], candidate: dict, job: dict, ref_count: int) -> dict:
    ordered = sorted(findings, key=lambda item: (item["code"], item["path"], item["message"]))
    return {
        "schema_version": "evidence-grounding-validation-report.v1",
        "status": "R0_FAIL_GROUNDING_CONTRACT" if ordered else "R0_PASS",
        "job_id": job.get("job_id"),
        "candidate_job_id": candidate.get("job_id"),
        "evidence_ref_count": ref_count,
        "finding_count": len(ordered),
        "findings": ordered,
    }


def write_report(path: Path | None, payload: dict) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path is None:
        sys.stdout.write(serialized)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate evidence_candidate.v2 grounding deterministically.")
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--packet-root", required=True, type=Path)
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "schemas"
        / "evidence"
        / "evidence_candidate.v2.schema.json",
    )
    parser.add_argument("--report-json", type=Path)
    args = parser.parse_args()
    try:
        payload = validate(
            load_json(args.job),
            load_json(args.candidate),
            args.packet_root.resolve(),
            load_json(args.schema),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    write_report(args.report_json, payload)
    return 0 if payload["status"] == "R0_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
