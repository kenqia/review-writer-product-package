"""Evidence-preserving rubric and Hard Fail projection for review releases."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from jsonschema import Draft202012Validator

from review_writer.project.manuscript_v2 import manuscript_state
from review_writer.project.source_truth import canonical_digest
from review_writer.delivery.chemical_paper_release import (
    ChemicalPaperReleaseError,
    analyze_chemical_paper_release,
    dependency_currentness_for_project,
    safe_chemical_paper_projection,
)
from review_writer.delivery.dual_parse_release import dual_parse_release_state
from review_writer.project.paper_evidence import (
    HONEST_PROGRESSIVE_ROUTE,
    HONEST_PROGRESSIVE_COVERAGE_THRESHOLD,
    PaperEvidenceError,
    honest_progressive_summary_from_projection,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_SCHEMA = REPO_ROOT / "schemas/quality/review_benchmark_report.v1.schema.json"
PLACEHOLDER_SCHEMA = REPO_ROOT / "schemas/figures/synthesis_figure_placeholder.v1.schema.json"
FIGURE_VERIFICATION_PATH = Path("03_figures/synthesis_figure_verification.json")
VERIFICATION_DECISION_SCHEMA = REPO_ROOT / "schemas/project/verification_decision.v1.schema.json"
RUBRIC_DIMENSIONS = (
    ("scope_and_question_value", 10),
    ("source_set_coverage", 15),
    ("evidence_fidelity", 20),
    ("synthesis_and_critique", 20),
    ("structure_and_narrative", 15),
    ("figure_information_value", 10),
    ("citation_and_traceability", 10),
)
COMPARISON_METRICS = (
    "section_proportions",
    "comparison_and_critique_density",
    "source_figure_density",
    "caption_information_content",
    "citation_density",
    "claim_traceability",
)
COMMON_HARD_FAILS = frozenset(
    {
        "WRONG_SOURCE_BINDING",
        "SUPPORTING_SOURCE_UNREAD",
        "HIGH_RISK_CLAIM_UNAPPROVED",
        "STALE_APPROVAL",
        "FABRICATED_SCIENTIFIC_DETAIL",
        "STATE_SURFACE_DIVERGENCE",
        "UNSOURCED_SCIENTIFIC_CLAIM",
        "LEGACY_DRAFT_REPACKAGED",
        "SYSTEM_GENERATED_SYNTHESIS_FIGURE",
        "DUAL_PARSE_STALE",
        "CORE_GENERIC_PARSE_MISSING_OR_STALE",
        "CORE_CHEMICAL_IMPORT_MISSING_OR_STALE",
        "CHEMICAL_COMPLETION_INCOMPLETE",
        "PARSE_RECONCILIATION_UNRESOLVED",
        "DUAL_SOURCE_BINDING_MISMATCH",
        "STALE_DUAL_PARSE_CONTENT_RESULT",
        "AI_AUTHORED_SMILES",
        "REACTION_ABSENCE_MISREPRESENTED",
    }
)
EXPERT_HARD_FAILS = frozenset(
    {"SYNTHESIS_FIGURE_PENDING", "CHEMICAL_DEPENDENCY_UNRESOLVED"}
)
RELEASE_LEVELS = frozenset({"SELF_REVIEWED_DRAFT", "EXPERT_REVIEWED_RELEASE"})


class BenchmarkError(ValueError):
    """A stable benchmark input or report failure."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def evaluate_honest_progressive(summary: object) -> dict[str, Any]:
    """Evaluate coverage, source traceability, and gap honesty only."""

    try:
        normalized = honest_progressive_summary_from_projection(
            summary, project_scope=True
        )
    except PaperEvidenceError as exc:
        raise BenchmarkError(exc.code, "Honest Progressive summary is invalid") from exc
    if normalized is None:
        raise BenchmarkError("HONEST_PROGRESSIVE_SUMMARY_INVALID")
    core_count = normalized["core_molecule_count"]
    traceability_rows = normalized["traceability"]
    traceable_count = sum(
        isinstance(row, dict)
        and isinstance(row.get("source_id"), str)
        and isinstance(row.get("pdf_locator"), dict)
        and bool(row.get("pdf_locator"))
        and isinstance(row.get("provenance"), (dict, list))
        and bool(row.get("provenance"))
        for row in traceability_rows
    )
    source_traceability = (
        1.0 if core_count == 0 else min(1.0, traceable_count / core_count)
    )
    gaps = normalized["gap_registry"]
    gap_honesty = (
        len(gaps) == normalized["blocked_count"]
        and all(
            isinstance(gap, dict)
            and gap.get("status") == "BLOCKED"
            and isinstance(gap.get("reason"), str)
            and bool(gap["reason"].strip())
            for gap in gaps
        )
    )
    status = (
        "pass_internal"
        if normalized["coverage_ratio"] >= HONEST_PROGRESSIVE_COVERAGE_THRESHOLD
        and source_traceability >= HONEST_PROGRESSIVE_COVERAGE_THRESHOLD
        and gap_honesty
        else "needs_revision"
    )
    return {
        "route": HONEST_PROGRESSIVE_ROUTE,
        "status": status,
        "core_molecule_count": normalized["core_molecule_count"],
        "confirmed_count": normalized["confirmed_count"],
        "ai_provisional_count": normalized["ai_provisional_count"],
        "blocked_count": normalized["blocked_count"],
        "coverage_ratio": normalized["coverage_ratio"],
        "coverage_threshold": normalized["coverage_threshold"],
        "coverage_sufficient": normalized["coverage_sufficient"],
        "source_traceability": source_traceability,
        "gap_honesty": gap_honesty,
        "paper_coverage": normalized["paper_coverage"],
        "uncertainty_statement": normalized["uncertainty_statement"],
        "gap_registry": normalized["gap_registry"],
        "traceability": normalized["traceability"],
        "actor_provenance_residual": normalized["actor_provenance_residual"],
        "credits_status": "NOT_APPLICABLE_BY_CURRENT_SCOPE",
    }


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _read_json(path: Path, code: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise BenchmarkError(code)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(code) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _verified_figure_bindings(
    project: Path,
    placeholders: list[dict[str, Any]],
    *,
    placeholder_digest: str,
    lineage_digest: object,
    manuscript_text: str,
 ) -> list[dict[str, str]] | None:
    verified = [row for row in placeholders if row.get("status") == "verified"]
    if not verified:
        return []
    try:
        payload = _read_json(project / FIGURE_VERIFICATION_PATH, "FIGURE_VERIFICATION_INVALID")
        decision_schema = _read_json(VERIFICATION_DECISION_SCHEMA, "BENCHMARK_SCHEMA_INVALID")
    except BenchmarkError:
        return None
    if not isinstance(payload, dict) or set(payload) != {"verifications"}:
        return None
    records = payload.get("verifications")
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        return None
    validator = Draft202012Validator(decision_schema)
    bindings: list[dict[str, str]] = []
    for placeholder in verified:
        placeholder_id = placeholder.get("placeholder_id")
        matches = [row for row in records if row.get("placeholder_id") == placeholder_id]
        if len(matches) != 1:
            return None
        record = matches[0]
        if set(record) != {
            "placeholder_id",
            "asset_path",
            "asset_sha256",
            "placeholder_digest",
            "lineage_digest",
            "verification",
        }:
            return None
        if (
            record["placeholder_digest"] != placeholder_digest
            or record["lineage_digest"] != lineage_digest
            or not isinstance(record["asset_path"], str)
            or not _sha256(record["asset_sha256"])
            or not isinstance(record["verification"], dict)
        ):
            return None
        verification = record["verification"]
        verification_object = {
            "placeholder_digest": record["placeholder_digest"],
            "placeholder_id": record["placeholder_id"],
            "asset_path": record["asset_path"],
            "asset_sha256": record["asset_sha256"],
            "lineage_digest": record["lineage_digest"],
        }
        if (
            list(validator.iter_errors(verification))
            or verification.get("actor_type") != "human_researcher"
            or verification.get("action") != "verify"
            or verification.get("bound_object_digest") != canonical_digest(verification_object)
            or verification.get("bound_gate_digest") != lineage_digest
        ):
            return None
        asset = project / record["asset_path"]
        try:
            resolved = asset.resolve(strict=True)
            resolved.relative_to(project)
            if asset.is_symlink() or not resolved.is_file():
                return None
            if _sha256_file(resolved) != record["asset_sha256"]:
                return None
            if f"HUMAN_SYNTHESIS_FIGURE: {record['asset_path']}" not in manuscript_text:
                return None
            bindings.append(
                {
                    "placeholder_id": str(placeholder_id),
                    "asset_path": record["asset_path"],
                    "asset_sha256": record["asset_sha256"],
                }
            )
        except (OSError, ValueError, KeyError):
            return None
    return bindings


def verified_synthesis_figure_bindings(
    project: Path,
    *,
    placeholders: list[dict[str, Any]],
    lineage_digest: str,
    manuscript_text: str,
) -> list[dict[str, str]] | None:
    """Return human-verified synthesis assets without weakening the decision boundary."""
    if not all(isinstance(row, dict) and row.get("status") == "verified" for row in placeholders):
        return None
    return _verified_figure_bindings(
        project,
        placeholders,
        placeholder_digest=canonical_digest(placeholders),
        lineage_digest=lineage_digest,
        manuscript_text=manuscript_text,
    )


def _verified_figure_state(
    project: Path,
    placeholders: list[dict[str, Any]],
    *,
    placeholder_digest: str,
    lineage_digest: object,
    manuscript_text: str,
    docx_path: Path | None,
) -> bool:
    bindings = _verified_figure_bindings(
        project,
        placeholders,
        placeholder_digest=placeholder_digest,
        lineage_digest=lineage_digest,
        manuscript_text=manuscript_text,
    )
    if bindings is None:
        return False
    if not bindings:
        return True
    if docx_path is None:
        return False
    try:
        with ZipFile(docx_path) as package:
            media_hashes = {
                hashlib.sha256(package.read(name)).hexdigest()
                for name in package.namelist()
                if name.startswith("word/media/") and not name.endswith("/")
            }
    except (OSError, BadZipFile, KeyError):
        return False
    return all(binding["asset_sha256"] in media_hashes for binding in bindings)


def verified_synthesis_figures_current(
    project: Path,
    *,
    lineage_digest: str,
    manuscript_text: str,
    docx_path: Path,
) -> bool:
    """Return whether every verified placeholder remains human-bound and embedded."""
    try:
        state = _read_json(
            project / "03_figures/synthesis_figure_placeholders.json",
            "FIGURE_PLACEHOLDER_INVALID",
        )
    except BenchmarkError:
        return False
    placeholders = state.get("placeholders") if isinstance(state, dict) else None
    if not isinstance(placeholders, list) or not all(isinstance(row, dict) for row in placeholders):
        return False
    placeholder_digest = canonical_digest(placeholders)
    if any(row.get("status") != "verified" for row in placeholders):
        return False
    return _verified_figure_state(
        project,
        placeholders,
        placeholder_digest=placeholder_digest,
        lineage_digest=lineage_digest,
        manuscript_text=manuscript_text,
        docx_path=docx_path,
    )


def _release_payload(release: Path, release_level: str | None) -> dict[str, Any]:
    """Read a release and bind the report to canonical, on-disk state."""
    if not isinstance(release, Path) or release.is_symlink() or not release.is_dir():
        raise BenchmarkError("BENCHMARK_RELEASE_INVALID")
    try:
        project = release.resolve(strict=True)
        snapshot = _read_json(project / "05_release/release_snapshot.json", "BENCHMARK_RELEASE_INVALID")
    except (OSError, BenchmarkError) as exc:
        raise BenchmarkError("BENCHMARK_RELEASE_INVALID") from exc
    if not isinstance(snapshot, dict):
        raise BenchmarkError("BENCHMARK_RELEASE_INVALID")
    project_id = snapshot.get("project_id", project.name)
    authoritative_level = snapshot.get("release_level") or snapshot.get("status")
    if release_level is not None and release_level != authoritative_level:
        raise BenchmarkError("BENCHMARK_RELEASE_LEVEL_MISMATCH")
    level = authoritative_level
    if level not in RELEASE_LEVELS or not isinstance(project_id, str) or not project_id:
        raise BenchmarkError("BENCHMARK_RELEASE_INVALID")

    divergence = False
    manuscript_path = project / "04_manuscript/manuscript.md"
    lineage_path = project / "04_manuscript/manuscript_lineage.v2.json"
    state: dict[str, Any]
    try:
        state = manuscript_state(project)
    except Exception as exc:
        raise BenchmarkError("BENCHMARK_MANUSCRIPT_NOT_APPROVED") from exc
    if state.get("workflow_can_continue") is not True:
        raise BenchmarkError("BENCHMARK_MANUSCRIPT_NOT_APPROVED")
    try:
        actual_manuscript_sha256 = _sha256_file(manuscript_path)
        lineage = _read_json(lineage_path, "BENCHMARK_RELEASE_INVALID")
        if not isinstance(lineage, dict):
            raise BenchmarkError("BENCHMARK_RELEASE_INVALID")
    except (OSError, BenchmarkError) as exc:
        raise BenchmarkError("BENCHMARK_MANUSCRIPT_NOT_APPROVED") from exc
    state_manuscript_sha256 = state.get("manuscript_sha256")
    state_lineage_digest = state.get("lineage_digest")
    if (
        not isinstance(actual_manuscript_sha256, str)
        or state_manuscript_sha256 != actual_manuscript_sha256
        or lineage.get("manuscript_sha256") != actual_manuscript_sha256
        or lineage.get("lineage_digest") != state_lineage_digest
    ):
        divergence = True

    docx_path: Path | None = None
    docx_sha256: str | None = None
    declared_docx_path = snapshot.get("docx_path")
    if isinstance(declared_docx_path, str) and declared_docx_path:
        candidate = project / declared_docx_path
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(project)
            if resolved.is_file() and not candidate.is_symlink():
                docx_path = resolved
                docx_sha256 = _sha256_file(resolved)
        except (OSError, ValueError):
            pass
    if (
        docx_path is None
        or snapshot.get("docx_sha256") != docx_sha256
        or snapshot.get("manuscript_sha256") != actual_manuscript_sha256
        or snapshot.get("lineage_digest") != state_lineage_digest
        or snapshot.get("project_id", project.name) != project_id
    ):
        divergence = True

    placeholders: list[dict[str, Any]] = []
    placeholder_path = project / "03_figures/synthesis_figure_placeholders.json"
    try:
        placeholder_state = _read_json(placeholder_path, "BENCHMARK_RELEASE_INVALID")
        if not isinstance(placeholder_state, dict) or not isinstance(placeholder_state.get("placeholders"), list):
            raise BenchmarkError("BENCHMARK_RELEASE_INVALID")
        placeholders = placeholder_state["placeholders"]
    except BenchmarkError:
        divergence = True
    validator = Draft202012Validator(_read_json(PLACEHOLDER_SCHEMA, "BENCHMARK_SCHEMA_INVALID"))
    if any(not isinstance(row, dict) or list(validator.iter_errors(row)) for row in placeholders):
        divergence = True
    placeholder_digest = canonical_digest(placeholders)
    if lineage.get("synthesis_figure_placeholder_digest") != placeholder_digest:
        divergence = True
    manuscript_text = ""
    try:
        manuscript_text = manuscript_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BenchmarkError("BENCHMARK_MANUSCRIPT_NOT_APPROVED") from exc
    try:
        chemical_state = analyze_chemical_paper_release(lineage)
        chemical_currentness = dependency_currentness_for_project(
            project, chemical_state
        )
    except ChemicalPaperReleaseError:
        chemical_state = analyze_chemical_paper_release({})
        chemical_currentness = {
            "lineage_binding_status": "stale",
            "can_release": False,
        }
        divergence = True
    chemical_paper_safe_summary = safe_chemical_paper_projection(chemical_state)
    if chemical_state["status"] == "available":
        if (
            chemical_currentness.get("lineage_binding_status") != "current"
            or snapshot.get("chemical_paper_binding_digest")
            != chemical_state["binding_digest"]
            or snapshot.get("chemical_paper_safe_summary")
            != chemical_paper_safe_summary
            or snapshot.get("chemical_paper_dependency_can_release")
            is not chemical_currentness.get("can_release")
        ):
            divergence = True
    elif (
        snapshot.get("chemical_paper_binding_digest") not in {None, ""}
        or snapshot.get("chemical_paper_safe_summary") is not None
        or snapshot.get("chemical_paper_dependency_can_release", True) is not True
    ):
        divergence = True
    dual_status = "not_applicable"
    dual_binding_digest: str | None = None
    reaction_data_status = "not_applicable"
    reaction_count: int | None = None
    dual_hard_fails: list[str] = []
    dual_source_root = project / "01_evidence/dual_source"
    has_dual_lineage = isinstance(lineage.get("dual_parse_bindings"), list)
    if dual_source_root.exists() or has_dual_lineage:
        dual_status = "stale"
        if (
            dual_source_root.is_symlink()
            or not dual_source_root.is_dir()
            or not has_dual_lineage
        ):
            dual_hard_fails.append("DUAL_PARSE_STALE")
            divergence = True
        else:
            expected_dual_digest = canonical_digest(lineage["dual_parse_bindings"])
            try:
                quality = _read_json(
                    project / "05_release/quality_report.json",
                    "BENCHMARK_RELEASE_INVALID",
                )
                dual = dual_parse_release_state(project)
            except Exception:
                quality = None
                dual = None
            if not isinstance(quality, dict) or not isinstance(dual, dict):
                dual_hard_fails.append("DUAL_PARSE_STALE")
                divergence = True
            else:
                raw_hard_fails = dual.get("hard_fails")
                if isinstance(raw_hard_fails, list) and all(
                    isinstance(code, str) for code in raw_hard_fails
                ):
                    dual_hard_fails.extend(raw_hard_fails)
                else:
                    dual_hard_fails.append("DUAL_PARSE_STALE")
                reaction_data_status = dual.get("reaction_data_status")
                reaction_count = dual.get("reaction_count")
                if (
                    dual.get("dual_parse_status") != "current"
                    or dual.get("internal_release_ready") is not True
                    or dual_hard_fails
                    or quality.get("dual_parse_status") != "current"
                    or quality.get("dual_parse_binding_digest")
                    != expected_dual_digest
                    or quality.get("reaction_data_status")
                    != reaction_data_status
                    or quality.get("reaction_count") != reaction_count
                    or quality.get("credits_status")
                    != "NOT_APPLICABLE_BY_CURRENT_SCOPE"
                    or reaction_data_status
                    not in {"available", "unavailable_not_provided"}
                    or (
                        reaction_data_status == "unavailable_not_provided"
                        and reaction_count is not None
                    )
                ):
                    if not dual_hard_fails:
                        dual_hard_fails.append("DUAL_PARSE_STALE")
                    divergence = True
                else:
                    dual_status = "current"
                    dual_binding_digest = expected_dual_digest
    for row in placeholders:
        if isinstance(row, dict) and row.get("status") != "verified":
            placeholder_id = row.get("placeholder_id")
            if (
                not isinstance(placeholder_id, str)
                or f"SYNTHESIS_FIGURE_PLACEHOLDER: {placeholder_id}" not in manuscript_text
            ):
                divergence = True

    verified_figure_ready = _verified_figure_state(
        project,
        placeholders,
        placeholder_digest=placeholder_digest,
        lineage_digest=state_lineage_digest,
        manuscript_text=manuscript_text,
        docx_path=docx_path,
    )
    if not verified_figure_ready:
        divergence = True

    signals = snapshot.get("hard_fail_signals", [])
    if not isinstance(signals, list) or not all(isinstance(code, str) for code in signals):
        raise BenchmarkError("BENCHMARK_RELEASE_INVALID")
    signals = list(signals)
    signals.extend(dual_hard_fails)
    if snapshot.get("system_generated_synthesis_figure") is True:
        signals.append("SYSTEM_GENERATED_SYNTHESIS_FIGURE")
    if divergence:
        signals.append("STATE_SURFACE_DIVERGENCE")
    try:
        honest_progressive = honest_progressive_summary_from_projection(
            snapshot, project_scope=True
        )
    except PaperEvidenceError as exc:
        raise BenchmarkError(
            exc.code, "Honest Progressive release projection is invalid"
        ) from exc
    return {
        "project_id": project_id,
        "release_level": level,
        "manuscript_sha256": actual_manuscript_sha256 if not divergence else None,
        "release_sha256": docx_sha256 if not divergence else None,
        "placeholders": placeholders,
        "verified_figure_ready": verified_figure_ready,
        "chemical_paper_state": chemical_state,
        "chemical_paper_safe_summary": chemical_paper_safe_summary,
        "chemical_paper_binding_digest": chemical_state["binding_digest"],
        "chemical_paper_dependency_can_release": chemical_currentness.get(
            "can_release", False
        ),
        "dual_parse_status": dual_status,
        "dual_parse_binding_digest": dual_binding_digest,
        "reaction_data_status": reaction_data_status,
        "reaction_count": reaction_count,
        "hard_fail_signals": signals,
        "honest_progressive": honest_progressive,
    }


def _rubric_rows(scores: object) -> list[dict[str, Any]]:
    if isinstance(scores, dict):
        source = scores.get("rubric", scores)
        if isinstance(source, dict):
            source = [
                {"dimension_id": dimension_id, **(value if isinstance(value, dict) else {"score": value})}
                for dimension_id, value in source.items()
            ]
    else:
        source = scores
    if not isinstance(source, list) or not all(isinstance(row, dict) for row in source):
        raise BenchmarkError("BENCHMARK_SCORES_INVALID")
    by_id = {row.get("dimension_id"): row for row in source}
    if len(by_id) != len(source) or set(by_id) != {key for key, _ in RUBRIC_DIMENSIONS}:
        raise BenchmarkError("BENCHMARK_SCORES_INVALID")
    rows: list[dict[str, Any]] = []
    for dimension_id, maximum in RUBRIC_DIMENSIONS:
        source_row = by_id[dimension_id]
        score = source_row.get("score")
        rationale = source_row.get("rationale")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or score < 0
            or score > maximum
            or not isinstance(rationale, str)
            or not rationale.strip()
            or source_row.get("max_score", maximum) != maximum
        ):
            raise BenchmarkError("BENCHMARK_SCORES_INVALID")
        rows.append(
            {
                "dimension_id": dimension_id,
                "max_score": maximum,
                "score": score,
                "rationale": rationale.strip(),
            }
        )
    return rows


def _tier(score: int | float) -> str:
    if score < 80:
        return "below_internal_threshold"
    if score < 90:
        return "acceptable_internal_revision_required"
    return "benchmark_internal"


def _expected_status(level: str, score: int | float, hard_fails: list[str]) -> str:
    if score < 80 or hard_fails:
        return "fail"
    return "pass_expert" if level == "EXPERT_REVIEWED_RELEASE" else "pass_internal"


def _schema_validator() -> Draft202012Validator:
    schema = _read_json(REPORT_SCHEMA, "BENCHMARK_SCHEMA_INVALID")
    if not isinstance(schema, dict):
        raise BenchmarkError("BENCHMARK_SCHEMA_INVALID")
    return Draft202012Validator(schema)


def validate_report(report: object) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise BenchmarkError("BENCHMARK_REPORT_INVALID")
    errors = sorted(_schema_validator().iter_errors(report), key=lambda error: list(error.path))
    if errors:
        raise BenchmarkError("BENCHMARK_REPORT_INVALID")
    rows = report["rubric"]
    summary = report["chemical_paper_safe_summary"]
    expected_chemical_issues: set[str] = set()
    if isinstance(summary, dict):
        if summary["missing_name_count"] or summary["missing_resolved_smiles_count"]:
            expected_chemical_issues.add("CHEMICAL_FIELDS_UNRESOLVED")
        if summary["element_review_counts"]["not_reviewed"]:
            expected_chemical_issues.add("CHEMICAL_ELEMENTS_NOT_REVIEWED")
        if summary["reaction_data_status"] == "unavailable_not_provided":
            expected_chemical_issues.add("CHEMICAL_REACTION_DATA_UNAVAILABLE")
    dependency_can_release = report["release_binding"][
        "chemical_paper_dependency_can_release"
    ]
    if not dependency_can_release:
        expected_chemical_issues.add("CHEMICAL_DEPENDENCY_UNRESOLVED")
    actual_chemical_issues = {
        code for code in report["issues"] if code.startswith("CHEMICAL_")
    }
    dual_status = report.get("dual_parse_status", "not_applicable")
    dual_binding = report["release_binding"].get("dual_parse_binding_digest")
    reaction_status = report.get("reaction_data_status", "not_applicable")
    reaction_count = report.get("reaction_count")
    honest_mode = report.get("route") == HONEST_PROGRESSIVE_ROUTE
    honest_evaluation = (
        evaluate_honest_progressive(report) if honest_mode else None
    )
    expected_ids = [key for key, _ in RUBRIC_DIMENSIONS]
    if (
        [row["dimension_id"] for row in rows] != expected_ids
        or [row["max_score"] for row in rows] != [value for _, value in RUBRIC_DIMENSIONS]
        or report["score"] != sum(row["score"] for row in rows)
        or report["tier"] != _tier(report["score"])
        or (
            report["status"]
            != (
                honest_evaluation["status"]
                if honest_mode and honest_evaluation is not None
                else _expected_status(
                    report["release_level"], report["score"], report["hard_fails"]
                )
            )
        )
        or report["comparison_metrics"] != list(COMPARISON_METRICS)
        or actual_chemical_issues != expected_chemical_issues
        or report.get("credits_status", "NOT_APPLICABLE_BY_CURRENT_SCOPE")
        != "NOT_APPLICABLE_BY_CURRENT_SCOPE"
        or (dual_status == "current") != isinstance(dual_binding, str)
        or (
            dual_status == "not_applicable"
            and (dual_binding is not None or reaction_status != "not_applicable")
        )
        or (
            reaction_status in {"not_applicable", "unavailable_not_provided"}
            and reaction_count is not None
        )
        or (
            report["release_level"] == "SELF_REVIEWED_DRAFT"
            and any(code in EXPERT_HARD_FAILS for code in report["hard_fails"])
        )
        or (
            report["release_level"] == "EXPERT_REVIEWED_RELEASE"
            and "SYNTHESIS_FIGURE_PENDING" in report["issues"]
            and "SYNTHESIS_FIGURE_PENDING" not in report["hard_fails"]
        )
        or (
            report["release_level"] == "EXPERT_REVIEWED_RELEASE"
            and (
                "CHEMICAL_DEPENDENCY_UNRESOLVED" in report["issues"]
            )
            != (
                "CHEMICAL_DEPENDENCY_UNRESOLVED" in report["hard_fails"]
            )
        )
    ):
        raise BenchmarkError("BENCHMARK_REPORT_INCONSISTENT")
    expert_ready = (
        (
            honest_evaluation is not None
            and honest_evaluation["status"] == "pass_internal"
        )
        if honest_mode
        else report["score"] >= 80
        and not report["hard_fails"]
        and "SYNTHESIS_FIGURE_PENDING" not in report["issues"]
        and "CHEMICAL_DEPENDENCY_UNRESOLVED" not in report["issues"]
    ) and not report["hard_fails"] and dependency_can_release and (
        "SYNTHESIS_FIGURE_PENDING" not in report["issues"]
    )
    if report["expert_release_ready"] is not expert_ready:
        raise BenchmarkError("BENCHMARK_REPORT_INCONSISTENT")
    return report


def evaluate_review(
    release: Path,
    rubric_scores: object,
    *,
    hard_fails: list[str] | tuple[str, ...] = (),
    release_level: str | None = None,
    standard_corpus: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project numeric scores and explicit failure evidence into one report."""
    binding = _release_payload(release, release_level)
    rubric = _rubric_rows(rubric_scores)
    score = sum(row["score"] for row in rubric)
    pending = (
        any(row.get("status") != "verified" for row in binding["placeholders"])
        or not binding["verified_figure_ready"]
    )
    issues = ["SYNTHESIS_FIGURE_PENDING"] if pending else []
    issues.extend(binding["chemical_paper_state"]["issues"])
    if not binding["chemical_paper_dependency_can_release"]:
        issues.append("CHEMICAL_DEPENDENCY_UNRESOLVED")
    all_hard_fails = list(hard_fails) + binding["hard_fail_signals"]
    if binding["release_level"] == "EXPERT_REVIEWED_RELEASE" and pending:
        all_hard_fails.append("SYNTHESIS_FIGURE_PENDING")
    if (
        binding["release_level"] == "EXPERT_REVIEWED_RELEASE"
        and not binding["chemical_paper_dependency_can_release"]
    ):
        all_hard_fails.append("CHEMICAL_DEPENDENCY_UNRESOLVED")
    honest_evaluation: dict[str, Any] | None = None
    if binding.get("honest_progressive") is not None:
        honest_evaluation = evaluate_honest_progressive(
            binding["honest_progressive"]
        )
        if honest_evaluation["blocked_count"]:
            issues.append("HONEST_PROGRESSIVE_GAPS_PRESENT")
        if not honest_evaluation["coverage_sufficient"]:
            all_hard_fails.append("HONEST_PROGRESSIVE_COVERAGE_BELOW_THRESHOLD")
    allowed = COMMON_HARD_FAILS | EXPERT_HARD_FAILS
    if any(code not in allowed for code in all_hard_fails):
        raise BenchmarkError("BENCHMARK_HARD_FAIL_INVALID")
    unique_hard_fails = sorted(set(all_hard_fails))
    report = {
        "schema_version": "review-benchmark-report.v1",
        "evaluated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "project_id": binding["project_id"],
        "release_level": binding["release_level"],
        "status": _expected_status(binding["release_level"], score, unique_hard_fails),
        "score": score,
        "tier": _tier(score),
        "expert_release_ready": (
            score >= 80
            and not unique_hard_fails
            and not pending
            and binding["chemical_paper_dependency_can_release"]
        ),
        "rubric": rubric,
        "hard_fails": unique_hard_fails,
        "issues": sorted(set(issues)),
        "chemical_paper_safe_summary": binding["chemical_paper_safe_summary"],
        "dual_parse_status": binding["dual_parse_status"],
        "reaction_data_status": binding["reaction_data_status"],
        "reaction_count": binding["reaction_count"],
        "credits_status": "NOT_APPLICABLE_BY_CURRENT_SCOPE",
        "release_binding": {
            "manuscript_sha256": binding["manuscript_sha256"],
            "release_sha256": binding["release_sha256"],
            "chemical_paper_binding_digest": binding[
                "chemical_paper_binding_digest"
            ],
            "chemical_paper_dependency_can_release": binding[
                "chemical_paper_dependency_can_release"
            ],
            "dual_parse_binding_digest": binding["dual_parse_binding_digest"],
        },
        "standard_corpus": standard_corpus,
        "comparison_metrics": list(COMPARISON_METRICS),
        "human_review_required": True,
        "disclaimer": "Regression score only; not scientific correctness, expert acceptance, or publication approval.",
    }
    if honest_evaluation is not None:
        report.update(
            {
                "route": HONEST_PROGRESSIVE_ROUTE,
                "status": (
                    "fail"
                    if unique_hard_fails
                    else honest_evaluation["status"]
                ),
                "expert_release_ready": (
                    honest_evaluation["status"] == "pass_internal"
                    and not unique_hard_fails
                    and not pending
                    and binding["chemical_paper_dependency_can_release"]
                ),
                "core_molecule_count": honest_evaluation["core_molecule_count"],
                "confirmed_count": honest_evaluation["confirmed_count"],
                "ai_provisional_count": honest_evaluation["ai_provisional_count"],
                "blocked_count": honest_evaluation["blocked_count"],
                "coverage_ratio": honest_evaluation["coverage_ratio"],
                "coverage_threshold": honest_evaluation["coverage_threshold"],
                "coverage_sufficient": honest_evaluation["coverage_sufficient"],
                "source_traceability": honest_evaluation["source_traceability"],
                "gap_honesty": honest_evaluation["gap_honesty"],
                "paper_coverage": honest_evaluation["paper_coverage"],
                "uncertainty_statement": honest_evaluation["uncertainty_statement"],
                "gap_registry": honest_evaluation["gap_registry"],
                "traceability": honest_evaluation["traceability"],
                "actor_provenance_residual": honest_evaluation[
                    "actor_provenance_residual"
                ],
                "disclaimer": (
                    "Coverage and provenance evaluation only; AI-provisional values "
                    "remain excluded from exact conclusions and blocked gaps remain disclosed."
                ),
            }
        )
    return validate_report(report)
