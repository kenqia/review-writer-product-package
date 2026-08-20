"""Strict release consumer for Scientific authority Chemical Paper bindings."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from review_writer.project.source_truth import canonical_digest


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^(?!\.\.?$)(?!.*[/\\\x00\r\n\s])[^/\\\x00\r\n]{1,240}$")
_IMPORT_KEYS = frozenset({"study_id", "import_digest", "state_digest"})
_SUMMARY_KEYS = frozenset(
    {
        "schema_version",
        "route",
        "study_count",
        "molecule_count",
        "missing_name_count",
        "missing_resolved_smiles_count",
        "ai_authored_smiles_count",
        "element_review_counts",
        "reaction_data_status",
    }
)
_ELEMENT_REVIEW_STATES = (
    "not_reviewed",
    "confirmed",
    "corrected",
    "not_applicable",
)
_CLAIM_KEYS = frozenset(
    {
        "claim_id",
        "study_id",
        "molecule_index",
        "required_fields",
        "requires_element_review",
        "requires_reaction_data",
    }
)
_CHEMICAL_FIELDS = frozenset({"resolved_smiles"})
_LINEAGE_KEYS = (
    "chemical_paper_import_digests",
    "chemical_paper_safe_summary",
    "chemical_paper_claim_dependencies",
)
_CURRENTNESS_KEYS = frozenset(
    {
        "schema_version",
        "lineage_binding_status",
        "claims",
        "can_release",
        "blocking_reasons",
    }
)
_CURRENT_CLAIM_KEYS = frozenset(
    {"claim_id", "status", "dependencies", "blocking_reasons"}
)
_CURRENT_DEPENDENCY_KEYS = frozenset(
    {
        "study_id",
        "molecule_index",
        "status",
        "required_field_statuses",
        "element_review_state",
        "reaction_data_status",
        "blocking_reasons",
    }
)
_CURRENT_STATUSES = frozenset(
    {"current", "stale", "missing", "needs_review", "unavailable"}
)
_FIELD_STATUSES = frozenset({"resolved", "corrected", "unresolved"})
_ELEMENT_STATES = frozenset(
    {"not_reviewed", "confirmed", "corrected", "not_applicable"}
)


class ChemicalPaperReleaseError(ValueError):
    """The frozen Scientific authority binding cannot be consumed safely."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _invalid() -> ChemicalPaperReleaseError:
    return ChemicalPaperReleaseError("CHEMICAL_PAPER_LINEAGE_INVALID")


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise _invalid()
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise _invalid()
    return value


def _nonnegative(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid()
    return value


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": "chemical-paper-release-evaluation.v1",
        "status": "not_applicable",
        "source_authority": "ORIGINAL_PDF",
        "chemical_paper_role": "MANUAL_EXPORT_PARSE_AID",
        "study_import_count": 0,
        "molecule_count": None,
        "missing_name_count": None,
        "missing_resolved_smiles_count": None,
        "ai_authored_smiles_count": None,
        "element_review_counts": None,
        "reaction_data_status": "not_applicable",
        "issues": [],
        "binding_digest": None,
        "claim_dependency_count": 0,
        "claim_dependencies": [],
        "import_digests": [],
    }


def _validate_imports(value: object) -> tuple[list[dict[str, str]], set[str]]:
    if not isinstance(value, list) or not value:
        raise _invalid()
    rows: list[dict[str, str]] = []
    study_ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != _IMPORT_KEYS:
            raise _invalid()
        row = {
            "study_id": _identifier(item.get("study_id")),
            "import_digest": _digest(item.get("import_digest")),
            "state_digest": _digest(item.get("state_digest")),
        }
        if row["study_id"] in study_ids:
            raise _invalid()
        study_ids.add(row["study_id"])
        rows.append(row)
    if [row["study_id"] for row in rows] != sorted(study_ids):
        raise _invalid()
    return rows, study_ids


def _validate_summary(value: object, *, study_count: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _SUMMARY_KEYS:
        raise _invalid()
    if (
        value.get("schema_version") != "chemical-paper-safe-summary.v2"
        or value.get("route") != "chemical-paper-zip-only"
        or _nonnegative(value.get("study_count")) != study_count
    ):
        raise _invalid()
    molecule_count = _nonnegative(value.get("molecule_count"))
    missing_name_count = _nonnegative(value.get("missing_name_count"))
    missing_resolved_smiles_count = _nonnegative(
        value.get("missing_resolved_smiles_count")
    )
    ai_authored_smiles_count = _nonnegative(value.get("ai_authored_smiles_count"))
    if missing_resolved_smiles_count > molecule_count:
        raise _invalid()
    counts = value.get("element_review_counts")
    if not isinstance(counts, dict) or set(counts) != set(_ELEMENT_REVIEW_STATES):
        raise _invalid()
    normalized_counts = {
        state: _nonnegative(counts.get(state)) for state in _ELEMENT_REVIEW_STATES
    }
    if sum(normalized_counts.values()) != molecule_count:
        raise _invalid()
    reaction_data_status = value.get("reaction_data_status")
    if reaction_data_status not in {"available", "unavailable_not_provided"}:
        raise _invalid()
    return {
        "schema_version": "chemical-paper-safe-summary.v2",
        "route": "chemical-paper-zip-only",
        "study_count": study_count,
        "molecule_count": molecule_count,
        "missing_name_count": missing_name_count,
        "missing_resolved_smiles_count": missing_resolved_smiles_count,
        "ai_authored_smiles_count": ai_authored_smiles_count,
        "element_review_counts": normalized_counts,
        "reaction_data_status": reaction_data_status,
    }


def _validate_claim_dependencies(
    value: object, *, study_ids: set[str]
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise _invalid()
    rows: list[dict[str, Any]] = []
    identities: list[tuple[str, str, int]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != _CLAIM_KEYS:
            raise _invalid()
        claim_id = _identifier(item.get("claim_id"))
        study_id = _identifier(item.get("study_id"))
        molecule_index = _nonnegative(item.get("molecule_index"))
        required_fields = item.get("required_fields")
        requires_elements = item.get("requires_element_review")
        requires_reaction = item.get("requires_reaction_data")
        if (
            study_id not in study_ids
            or not isinstance(required_fields, list)
            or required_fields != sorted(set(required_fields))
            or any(field not in _CHEMICAL_FIELDS for field in required_fields)
            or not isinstance(requires_elements, bool)
            or not isinstance(requires_reaction, bool)
            or not (required_fields or requires_elements or requires_reaction)
        ):
            raise _invalid()
        row = {
            "claim_id": claim_id,
            "study_id": study_id,
            "molecule_index": molecule_index,
            "required_fields": list(required_fields),
            "requires_element_review": requires_elements,
            "requires_reaction_data": requires_reaction,
        }
        rows.append(row)
        identities.append((claim_id, study_id, molecule_index))
    if identities != sorted(set(identities)):
        raise _invalid()
    return rows


def analyze_chemical_paper_release(lineage: object) -> dict[str, Any]:
    """Validate frozen lineage fields and return an internal release state."""
    if not isinstance(lineage, dict):
        raise _invalid()
    if "chemical_paper_lineage" in lineage:
        raise _invalid()
    present = [key in lineage for key in _LINEAGE_KEYS]
    if not any(present):
        return _empty_state()
    if not all(present):
        raise _invalid()
    imports, study_ids = _validate_imports(lineage[_LINEAGE_KEYS[0]])
    summary = _validate_summary(
        lineage[_LINEAGE_KEYS[1]], study_count=len(imports)
    )
    claims = _validate_claim_dependencies(
        lineage[_LINEAGE_KEYS[2]], study_ids=study_ids
    )
    issues: list[str] = []
    if summary["missing_name_count"] or summary["missing_resolved_smiles_count"]:
        issues.append("CHEMICAL_FIELDS_UNRESOLVED")
    if summary["element_review_counts"]["not_reviewed"]:
        issues.append("CHEMICAL_ELEMENTS_NOT_REVIEWED")
    if summary["reaction_data_status"] == "unavailable_not_provided":
        issues.append("CHEMICAL_REACTION_DATA_UNAVAILABLE")
    issues.sort()
    return {
        "schema_version": "chemical-paper-release-evaluation.v1",
        "status": "available",
        "source_authority": "ORIGINAL_PDF",
        "chemical_paper_role": "MANUAL_EXPORT_PARSE_AID",
        "study_import_count": len(imports),
        "molecule_count": summary["molecule_count"],
        "missing_name_count": summary["missing_name_count"],
        "missing_resolved_smiles_count": summary["missing_resolved_smiles_count"],
        "ai_authored_smiles_count": summary["ai_authored_smiles_count"],
        "element_review_counts": summary["element_review_counts"],
        "reaction_data_status": summary["reaction_data_status"],
        "issues": issues,
        "binding_digest": canonical_digest(
            {
                "chemical_paper_import_digests": imports,
                "chemical_paper_safe_summary": summary,
                "chemical_paper_claim_dependencies": claims,
            }
        ),
        "claim_dependency_count": len(claims),
        "claim_dependencies": claims,
        "import_digests": imports,
    }


def safe_chemical_paper_projection(
    state: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the exact frozen safe summary, never authority digests or row IDs."""
    if state.get("status") != "available":
        return None
    return {
        "schema_version": "chemical-paper-safe-summary.v2",
        "route": "chemical-paper-zip-only",
        "study_count": state["study_import_count"],
        "molecule_count": state["molecule_count"],
        "missing_name_count": state["missing_name_count"],
        "missing_resolved_smiles_count": state["missing_resolved_smiles_count"],
        "ai_authored_smiles_count": state["ai_authored_smiles_count"],
        "element_review_counts": copy.deepcopy(state["element_review_counts"]),
        "reaction_data_status": state["reaction_data_status"],
    }


def _blocking_reasons(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ChemicalPaperReleaseError("CHEMICAL_PAPER_CURRENTNESS_INVALID")
    reasons: list[str] = []
    for item in value:
        try:
            reasons.append(_identifier(item))
        except ChemicalPaperReleaseError as exc:
            raise ChemicalPaperReleaseError(
                "CHEMICAL_PAPER_CURRENTNESS_INVALID"
            ) from exc
    if reasons != sorted(set(reasons)):
        raise ChemicalPaperReleaseError("CHEMICAL_PAPER_CURRENTNESS_INVALID")
    return reasons


def _currentness_invalid() -> ChemicalPaperReleaseError:
    return ChemicalPaperReleaseError("CHEMICAL_PAPER_CURRENTNESS_INVALID")


def validate_dependency_currentness(
    state: dict[str, Any], value: object
) -> dict[str, Any]:
    """Validate the exact Scientific authority dependency-currentness response."""
    if state.get("status") != "available":
        if value is not None:
            raise _currentness_invalid()
        return {
            "schema_version": "chemical-paper-dependency-currentness.v2",
            "lineage_binding_status": "missing",
            "claims": [],
            "can_release": True,
            "blocking_reasons": [],
            "blocked_claim_count": 0,
        }
    if not isinstance(value, dict) or set(value) != _CURRENTNESS_KEYS:
        raise _currentness_invalid()
    binding_status = value.get("lineage_binding_status")
    if (
        value.get("schema_version")
        != "chemical-paper-dependency-currentness.v2"
        or binding_status not in {"current", "stale", "missing"}
        or not isinstance(value.get("can_release"), bool)
        or not isinstance(value.get("claims"), list)
    ):
        raise _currentness_invalid()
    expected_by_claim: dict[str, list[dict[str, Any]]] = {}
    for dependency in state["claim_dependencies"]:
        expected_by_claim.setdefault(dependency["claim_id"], []).append(dependency)
    actual_claims: list[dict[str, Any]] = []
    claim_ids: list[str] = []
    for claim in value["claims"]:
        if not isinstance(claim, dict) or set(claim) != _CURRENT_CLAIM_KEYS:
            raise _currentness_invalid()
        try:
            claim_id = _identifier(claim.get("claim_id"))
        except ChemicalPaperReleaseError as exc:
            raise _currentness_invalid() from exc
        status = claim.get("status")
        dependencies = claim.get("dependencies")
        if status not in _CURRENT_STATUSES or not isinstance(dependencies, list):
            raise _currentness_invalid()
        expected_dependencies = expected_by_claim.get(claim_id)
        if expected_dependencies is None or len(dependencies) != len(expected_dependencies):
            raise _currentness_invalid()
        normalized_dependencies: list[dict[str, Any]] = []
        dependency_reasons: list[str] = []
        for expected, dependency in zip(expected_dependencies, dependencies, strict=True):
            if (
                not isinstance(dependency, dict)
                or set(dependency) != _CURRENT_DEPENDENCY_KEYS
            ):
                raise _currentness_invalid()
            try:
                study_id = _identifier(dependency.get("study_id"))
                molecule_index = _nonnegative(dependency.get("molecule_index"))
            except ChemicalPaperReleaseError as exc:
                raise _currentness_invalid() from exc
            dependency_status = dependency.get("status")
            field_statuses = dependency.get("required_field_statuses")
            element_state = dependency.get("element_review_state")
            reaction_status = dependency.get("reaction_data_status")
            reasons = _blocking_reasons(dependency.get("blocking_reasons"))
            if (
                study_id != expected["study_id"]
                or molecule_index != expected["molecule_index"]
                or dependency_status not in _CURRENT_STATUSES
                or not isinstance(field_statuses, dict)
                or (
                    dependency_status not in {"stale", "missing"}
                    and set(field_statuses) != set(expected["required_fields"])
                )
                or (
                    dependency_status in {"stale", "missing"}
                    and set(field_statuses) not in (set(), set(expected["required_fields"]))
                )
                or any(item not in _FIELD_STATUSES for item in field_statuses.values())
                or element_state not in _ELEMENT_STATES
                or reaction_status
                not in {"available", "unavailable_not_provided", "not_applicable"}
                or (
                    expected["requires_reaction_data"]
                    and reaction_status == "not_applicable"
                )
            ):
                raise _currentness_invalid()
            ready = (
                all(item != "unresolved" for item in field_statuses.values())
                and (
                    not expected["requires_element_review"]
                    or element_state in {"confirmed", "corrected", "not_applicable"}
                )
                and (
                    not expected["requires_reaction_data"]
                    or reaction_status == "available"
                )
            )
            if (
                (dependency_status == "current" and not ready)
                or (dependency_status == "current" and reasons)
                or (dependency_status != "current" and not reasons)
            ):
                raise _currentness_invalid()
            dependency_reasons.extend(reasons)
            normalized_dependencies.append(copy.deepcopy(dependency))
        claim_reasons = _blocking_reasons(claim.get("blocking_reasons"))
        expected_claim_reasons = sorted(set(dependency_reasons))
        priority = {
            "stale": 4,
            "missing": 3,
            "unavailable": 2,
            "needs_review": 1,
            "current": 0,
        }
        expected_claim_status = max(
            (row["status"] for row in normalized_dependencies),
            key=lambda item: priority[item],
        )
        if (
            claim_reasons != expected_claim_reasons
            or status != expected_claim_status
            or (status == "current" and claim_reasons)
        ):
            raise _currentness_invalid()
        actual_claims.append(
            {
                "claim_id": claim_id,
                "status": status,
                "dependencies": normalized_dependencies,
                "blocking_reasons": claim_reasons,
            }
        )
        claim_ids.append(claim_id)
    if claim_ids != sorted(expected_by_claim) or set(claim_ids) != set(expected_by_claim):
        raise _currentness_invalid()
    top_reasons = _blocking_reasons(value.get("blocking_reasons"))
    expected_top_reasons = sorted(
        {reason for claim in actual_claims for reason in claim["blocking_reasons"]}
    )
    if binding_status != "current":
        expected_top_reasons = sorted(
            {*expected_top_reasons, f"chemical_paper_lineage:{binding_status}"}
        )
    expected_release = (
        binding_status == "current"
        and all(claim["status"] == "current" for claim in actual_claims)
    )
    if top_reasons != expected_top_reasons or value["can_release"] is not expected_release:
        raise _currentness_invalid()
    return {
        "schema_version": "chemical-paper-dependency-currentness.v2",
        "lineage_binding_status": binding_status,
        "claims": actual_claims,
        "can_release": expected_release,
        "blocking_reasons": top_reasons,
        "blocked_claim_count": sum(
            claim["status"] != "current" for claim in actual_claims
        ),
    }


def dependency_currentness_for_project(
    project: Path, state: dict[str, Any]
) -> dict[str, Any]:
    """Ask Scientific authority to recompute currentness, then consume it strictly."""
    if state.get("status") != "available":
        return validate_dependency_currentness(state, None)
    try:
        from review_writer.project.chemical_paper import (
            chemical_paper_dependency_currentness,
        )

        value = chemical_paper_dependency_currentness(
            Path(project),
            import_digests=copy.deepcopy(state["import_digests"]),
            claim_dependencies=copy.deepcopy(state["claim_dependencies"]),
        )
    except (ImportError, OSError, ValueError, TypeError, KeyError) as exc:
        raise ChemicalPaperReleaseError(
            "CHEMICAL_PAPER_CURRENTNESS_UNAVAILABLE"
        ) from exc
    return validate_dependency_currentness(state, value)


def render_chemical_paper_limitations(state: dict[str, Any]) -> str:
    """Render explicit provenance/absence semantics into internal DOCX text."""
    if state.get("status") != "available":
        return ""
    lines = [
        "## Chemical Paper provenance and limitations",
        "",
        "Original PDFs remain the scientific source of truth. Chemical Paper output is a manual-export parsing aid, not scientific truth.",
        "",
        (
            f"Chemical Paper lineage covers {state['study_import_count']} study import(s) and "
            f"{state['molecule_count']} candidate molecule record(s); "
            f"{state['missing_name_count']} molecule name value(s) and "
            f"{state['missing_resolved_smiles_count']} authoritative SMILES value(s) remain unresolved, and "
            f"{state['element_review_counts']['not_reviewed']} molecule element record(s) remain not reviewed."
        ),
    ]
    if state["reaction_data_status"] == "unavailable_not_provided":
        lines.extend(
            [
                "",
                "Reaction data are unavailable/not provided; this does not mean zero confirmed reactions.",
            ]
        )
    if "CHEMICAL_DEPENDENCY_UNRESOLVED" in state.get("issues", []):
        lines.extend(
            [
                "",
                "One or more claim-dependent chemical values still require review; this internal document is not eligible for expert release.",
            ]
        )
    return "\n".join(lines) + "\n"


def release_markdown_with_chemical_limitations(
    markdown: str, state: dict[str, Any]
) -> str:
    limitation = render_chemical_paper_limitations(state)
    if not limitation:
        return markdown
    marker = "## Chemical Paper provenance and limitations"
    if marker in markdown:
        raise ChemicalPaperReleaseError("CHEMICAL_PAPER_LIMITATION_AMBIGUOUS")
    match = re.search(r"(?m)^##\s+References\s*$", markdown)
    if match:
        return (
            markdown[: match.start()].rstrip()
            + "\n\n"
            + limitation
            + "\n"
            + markdown[match.start() :]
        )
    return markdown.rstrip() + "\n\n" + limitation
