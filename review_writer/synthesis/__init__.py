"""Deterministic, source-bound candidate synthesis primitives.

This module deliberately does not read or write a project directory.  Callers
must provide explicit comparison positions and relations; the implementation
does not infer scientific meaning from prose.  The returned projection is a
candidate-only artifact and never promotes evidence or claims to a human or
scientific decision.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

SCHEMA_VERSION = "synthesis.v1"
SYNTHESIS_STATUS = "AI_PROVISIONAL"

VALID_STATUSES = frozenset(
    {"CONFIRMED", "AI_PROVISIONAL", "GAP", "NON_COMPARABLE", "BLOCKED"}
)
VALID_RELATIONS = frozenset({"supports", "counter", "conflict", "incomparable"})
CONTEXT_ONLY_ROLES = frozenset(
    {
        "BACKGROUND",
        "BACKGROUND_REVIEW",
        "CONTEXT_ONLY",
        "REVIEW",
        "SECONDARY_REVIEW",
    }
)


class SynthesisError(ValueError):
    """Stable, fail-closed input or projection error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical_digest(value: object) -> str:
    """Return the repository-compatible SHA-256 digest of JSON data."""

    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise SynthesisError("CANONICAL_VALUE_INVALID") from exc
    return hashlib.sha256(payload).hexdigest()


def _json_copy(value: object, code: str) -> object:
    try:
        copied = copy.deepcopy(value)
        json.dumps(
            copied,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError, copy.Error) as exc:
        raise SynthesisError(code) from exc
    return copied


def _text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SynthesisError(code)
    return value.strip()


def _is_context_only(source_role: str) -> bool:
    return source_role.strip().upper() in CONTEXT_ONLY_ROLES


def _normalize_expected_keys(expected_keys: Iterable[str] | None) -> list[str]:
    if expected_keys is None:
        return []
    if isinstance(expected_keys, (str, bytes, Mapping)):
        raise SynthesisError("EXPECTED_KEYS_INVALID")
    try:
        values = list(expected_keys)
    except TypeError as exc:
        raise SynthesisError("EXPECTED_KEYS_INVALID") from exc
    normalized: list[str] = []
    for value in values:
        normalized.append(_text(value, "EXPECTED_KEY_INVALID"))
    if len(set(normalized)) != len(normalized):
        raise SynthesisError("EXPECTED_KEY_DUPLICATE")
    return sorted(normalized)


def _normalize_evidence(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SynthesisError("EVIDENCE_INVALID")

    evidence_id = _text(value.get("evidence_id"), "EVIDENCE_ID_REQUIRED")
    source_id = _text(value.get("source_id"), "SOURCE_ID_REQUIRED")
    study_id = _text(value.get("study_id"), "STUDY_ID_REQUIRED")
    source_role = _text(value.get("source_role"), "SOURCE_ROLE_REQUIRED")
    status = _text(value.get("status"), "STATUS_REQUIRED")
    if status not in VALID_STATUSES:
        raise SynthesisError("STATUS_INVALID")
    comparison_key = _text(value.get("comparison_key"), "COMPARISON_KEY_REQUIRED")
    if "position" not in value or value.get("position") is None:
        raise SynthesisError("POSITION_REQUIRED")
    position = _json_copy(value["position"], "POSITION_INVALID")
    relation = _text(value.get("relation"), "RELATION_REQUIRED")
    if relation not in VALID_RELATIONS:
        raise SynthesisError("RELATION_INVALID")

    locator = value.get("locator")
    if not isinstance(locator, Mapping) or not locator:
        raise SynthesisError("TRACEABILITY_REQUIRED")
    locator_copy = _json_copy(locator, "TRACEABILITY_INVALID")

    provenance = value.get("provenance")
    if provenance is not None:
        provenance_copy = _json_copy(provenance, "PROVENANCE_INVALID")
    else:
        provenance_copy = {"locator": copy.deepcopy(locator_copy)}

    result: dict[str, Any] = {
        "evidence_id": evidence_id,
        "source_id": source_id,
        "study_id": study_id,
        "source_role": source_role,
        "status": status,
        "comparison_key": comparison_key,
        "position": position,
        "position_digest": canonical_digest(position),
        "relation": relation,
        "locator": locator_copy,
        "provenance": provenance_copy,
    }
    for optional_key in ("statement", "epistemic_type", "mechanism_grade"):
        if optional_key in value:
            optional_value = value[optional_key]
            if optional_key == "statement":
                optional_value = _text(optional_value, "STATEMENT_INVALID")
            else:
                optional_value = _json_copy(optional_value, "EVIDENCE_OPTIONAL_INVALID")
            result[optional_key] = optional_value
    return result


def _lineage(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(record[key])
        for key in (
            "evidence_id",
            "source_id",
            "study_id",
            "source_role",
            "status",
            "comparison_key",
            "position",
            "position_digest",
            "relation",
            "locator",
            "provenance",
        )
        if key in record
    }


def _position_groups(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["position_digest"]].append(record)
    groups: list[dict[str, Any]] = []
    for position_digest in sorted(grouped):
        values = sorted(grouped[position_digest], key=lambda item: item["evidence_id"])
        groups.append(
            {
                "position_digest": position_digest,
                "position": copy.deepcopy(values[0]["position"]),
                "evidence_ids": [item["evidence_id"] for item in values],
                "source_ids": sorted({item["source_id"] for item in values}),
                "study_ids": sorted({item["study_id"] for item in values}),
            }
        )
    return groups


def _row_for_key(comparison_key: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    records = sorted(records, key=lambda item: item["evidence_id"])
    lineage = [_lineage(record) for record in records]
    context_only_ids = [
        record["evidence_id"]
        for record in records
        if _is_context_only(record["source_role"])
    ]
    primary = [
        record
        for record in records
        if not _is_context_only(record["source_role"])
        and record["status"] not in {"GAP", "BLOCKED", "NON_COMPARABLE"}
        and record["relation"] != "incomparable"
    ]
    gap_ids = [
        record["evidence_id"]
        for record in records
        if record["status"] in {"GAP", "BLOCKED"}
    ]
    non_comparable_ids = [
        record["evidence_id"]
        for record in records
        if record["status"] == "NON_COMPARABLE"
        or record["relation"] == "incomparable"
    ]
    supporting_ids = [
        record["evidence_id"]
        for record in primary
        if record["relation"] == "supports"
    ]
    counter_ids = [
        record["evidence_id"]
        for record in primary
        if record["relation"] in {"counter", "conflict"}
    ]
    conflict_ids = [
        record["evidence_id"]
        for record in primary
        if record["relation"] in {"counter", "conflict"}
    ]
    groups = _position_groups(primary)
    primary_source_ids = sorted({record["source_id"] for record in primary})

    if non_comparable_ids:
        classification = "NON_COMPARABLE"
        reason_code = "NON_COMPARABLE_EVIDENCE_PRESENT"
    elif not primary:
        classification = "GAP"
        reason_code = (
            "INSUFFICIENT_INDEPENDENT_PRIMARY_SOURCES"
            if context_only_ids
            else "NO_TRACEABLE_EVIDENCE"
        )
    elif not supporting_ids:
        classification = "GAP"
        reason_code = "COUNTER_EVIDENCE_WITHOUT_SUPPORT"
    elif len(primary_source_ids) < 2:
        classification = "GAP"
        reason_code = "INSUFFICIENT_INDEPENDENT_PRIMARY_SOURCES"
    elif conflict_ids:
        classification = "conflict"
        reason_code = "EXPLICIT_COUNTER_EVIDENCE"
    elif len(groups) == 1:
        classification = "consensus"
        reason_code = "CONSISTENT_COMPARABLE_POSITIONS"
    else:
        classification = "difference"
        reason_code = "DIVERGENT_COMPARABLE_POSITIONS"

    evidence_statuses = sorted({record["status"] for record in records})
    # The synthesis itself is always an AI candidate, even when every
    # upstream evidence row is human-confirmed.  GAP and NON_COMPARABLE stay
    # visible in the separate row status/classification fields.
    synthesis_status = SYNTHESIS_STATUS
    row_status = (
        classification
        if classification in {"GAP", "NON_COMPARABLE"}
        else synthesis_status
    )

    body: dict[str, Any] = {
        "comparison_key": comparison_key,
        "classification": classification,
        "status": row_status,
        "synthesis_status": synthesis_status,
        "reason_code": reason_code,
        "supporting_evidence_ids": sorted(supporting_ids),
        "counter_evidence_ids": sorted(counter_ids),
        "conflict_evidence_ids": sorted(conflict_ids),
        "gap_evidence_ids": sorted(gap_ids),
        "non_comparable_evidence_ids": sorted(non_comparable_ids),
        "context_only_evidence_ids": sorted(context_only_ids),
        "position_groups": groups,
        "source_ids": primary_source_ids,
        "study_ids": sorted({record["study_id"] for record in primary}),
        "source_roles": sorted({record["source_role"] for record in records}),
        "evidence_statuses": evidence_statuses,
        "divergent_lineage": len({record["source_id"] for record in records}) > 1
        or len(groups) > 1,
        "lineage": lineage,
    }
    body["lineage_digest"] = canonical_digest(lineage)
    synthesis_digest = canonical_digest(body)
    body["synthesis_id"] = f"synthesis-{synthesis_digest[:24]}"
    body["synthesis_digest"] = canonical_digest(body)
    return body


def synthesize(
    evidence: Iterable[Mapping[str, Any]],
    *,
    expected_keys: Iterable[str] | None = None,
    comparison_id: str = "adaptive-synthesis",
) -> dict[str, Any]:
    """Build a deterministic, traceable candidate synthesis in memory.

    Each evidence row must explicitly provide ``evidence_id``, ``source_id``,
    ``study_id``, ``source_role``, ``status``, ``comparison_key``, ``position``,
    ``relation`` and a non-empty ``locator``.  ``supports``/``counter``/
    ``conflict`` are interpreted only as caller-provided relations; this
    function does not infer them from text.
    """

    if isinstance(evidence, (str, bytes, Mapping)):
        raise SynthesisError("EVIDENCE_INVALID")
    try:
        raw_rows = list(evidence)
    except TypeError as exc:
        raise SynthesisError("EVIDENCE_INVALID") from exc
    expected = _normalize_expected_keys(expected_keys)
    if not raw_rows and not expected:
        raise SynthesisError("EVIDENCE_REQUIRED")

    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_rows:
        record = _normalize_evidence(raw)
        if record["evidence_id"] in seen_ids:
            raise SynthesisError("EVIDENCE_ID_DUPLICATE")
        seen_ids.add(record["evidence_id"])
        records.append(record)
    records.sort(key=lambda item: (item["comparison_key"], item["evidence_id"]))

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["comparison_key"]].append(record)
    all_keys = sorted(set(grouped) | set(expected))
    rows = [
        _row_for_key(comparison_key, grouped.get(comparison_key, []))
        if comparison_key in grouped
        else _row_for_key(comparison_key, [])
        for comparison_key in all_keys
    ]

    role_index: dict[str, list[str]] = defaultdict(list)
    for record in records:
        role_index[record["source_role"]].append(record["evidence_id"])
    source_roles = {
        role: sorted(ids) for role, ids in sorted(role_index.items(), key=lambda item: item[0])
    }
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "comparison_id": _text(comparison_id, "COMPARISON_ID_INVALID"),
        "expected_keys": expected,
        "status": "candidate_only",
        "synthesis_status": SYNTHESIS_STATUS,
        "workflow_can_continue": False,
        "reason_code": "SYNTHESIS_CANDIDATE_ONLY",
        "rows": rows,
        "classification_counts": {
            classification: sum(
                row["classification"] == classification for row in rows
            )
            for classification in (
                "consensus",
                "difference",
                "conflict",
                "NON_COMPARABLE",
                "GAP",
            )
            if any(row["classification"] == classification for row in rows)
        },
        "source_roles": source_roles,
        "evidence_ids": sorted(record["evidence_id"] for record in records),
        "acceptance_layers": {
            "engineering": "NOT_EVALUATED",
            "product_use": "NOT_EVALUATED",
            "public_e2e": "NOT_EVALUATED",
            "independent_quality": "NOT_EVALUATED",
            "human_acceptance": "NOT_EVALUATED",
            "scientific_validity": "NOT_EVALUATED",
        },
    }
    body["projection_digest"] = canonical_digest(body)
    return copy.deepcopy(body)


def build_synthesis(
    evidence: Iterable[Mapping[str, Any]],
    *,
    expected_keys: Iterable[str] | None = None,
    comparison_id: str = "adaptive-synthesis",
) -> dict[str, Any]:
    """Compatibility alias for callers that prefer a builder verb."""

    return synthesize(
        evidence,
        expected_keys=expected_keys,
        comparison_id=comparison_id,
    )


__all__ = [
    "SCHEMA_VERSION",
    "SynthesisError",
    "build_synthesis",
    "canonical_digest",
    "synthesize",
]
