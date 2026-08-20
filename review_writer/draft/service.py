"""Evidence-aware Draft -> PDF metadata linkage and impact preview."""

from __future__ import annotations

import copy
import json
from collections import defaultdict, deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import (
    ACCEPTANCE_LAYERS,
    DEFAULT_ACCEPTANCE_LAYERS,
    IMPACT_PREVIEW_SCHEMA,
    NODE_ID_FIELDS,
    NODE_KINDS,
    PRESERVED_EVIDENCE_STATUSES,
    SCHEMA_VERSION,
    STALE_STATUSES,
    DraftBranchError,
    DraftConflictError,
    DraftHistoryError,
    DraftValidationError,
    DownloadArtifact,
    canonical_digest,
    copy_json,
    is_current,
    is_divergent,
    is_historical,
    is_writable,
    lineage_ids,
    node_identifier,
    source_roles,
    status_code,
    status_of,
    validate_identifier,
    validate_sha256,
)


NodeKey = tuple[str, str]


def _nonempty_text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DraftValidationError(code)
    return value


def _mapping(value: object, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise DraftValidationError(code)
    return value


def _rows(value: object, kind: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise DraftValidationError(f"{kind.upper()}_ROWS_INVALID")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise DraftValidationError(f"{kind.upper()}_ROW_INVALID")
        row = copy_json(dict(raw), code=f"{kind.upper()}_JSON_INVALID")
        identifier = node_identifier(kind, row)
        if identifier in seen:
            raise DraftConflictError(f"{kind.upper()}_DUPLICATE")
        seen.add(identifier)
        rows.append(row)
    return rows


def _ref_ids(
    record: Mapping[str, Any],
    fields: tuple[str, ...],
    *,
    code: str,
) -> tuple[str, ...]:
    value: object = None
    found = False
    for field in fields:
        if field in record:
            value = record[field]
            found = True
            break
    if not found:
        return ()
    if not isinstance(value, (list, tuple)):
        raise DraftValidationError(code)
    result: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            item = item.get("id") or item.get("evidence_id") or item.get("claim_id")
        result.append(validate_identifier(item, field="reference_id"))
    if len(result) != len(set(result)):
        raise DraftConflictError(code)
    return tuple(result)


def _evidence_refs(record: Mapping[str, Any]) -> tuple[str, ...]:
    return _ref_ids(record, ("evidence_ids", "evidence_refs"), code="EVIDENCE_REFS_INVALID")


def _claim_refs(record: Mapping[str, Any]) -> tuple[str, ...]:
    return _ref_ids(record, ("claim_ids", "claim_refs"), code="CLAIM_REFS_INVALID")


def _block_refs(record: Mapping[str, Any], field: str) -> tuple[str, ...]:
    aliases = (field, field.replace("_refs", "_ids"))
    return _ref_ids(record, aliases, code="DRAFT_BLOCK_REFS_INVALID")


def _lineage_present(record: Mapping[str, Any]) -> bool:
    return bool(lineage_ids(record))


def _evidence_is_stale(record: Mapping[str, Any]) -> bool:
    return bool(
        record.get("stale") is True
        or status_code(record.get("status")) in STALE_STATUSES
        or not is_current(record)
    )


def _validate_evidence(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        status_of(row)
        _nonempty_text(row.get("source_id"), "SOURCE_ID_REQUIRED")
        _nonempty_text(row.get("study_id"), "STUDY_ID_REQUIRED")
        if not source_roles(row):
            raise DraftValidationError("SOURCE_ROLE_REQUIRED")
        if not _lineage_present(row):
            raise DraftValidationError("LINEAGE_REQUIRED")
        _mapping(row.get("locator"), "EVIDENCE_LOCATOR_REQUIRED")


def _require_evidence(
    evidence: dict[str, dict[str, Any]],
    evidence_id: str,
    *,
    owner_historical: bool,
) -> dict[str, Any]:
    row = evidence.get(evidence_id)
    if row is None:
        raise DraftValidationError("CLAIM_EVIDENCE_NOT_FOUND")
    if not owner_historical and _evidence_is_stale(row):
        raise DraftValidationError("CLAIM_EVIDENCE_STALE")
    if not _lineage_present(row) or not source_roles(row):
        raise DraftValidationError("CLAIM_EVIDENCE_NOT_SOURCE_BOUND")
    return row


def _validate_claims(
    rows: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
) -> None:
    for row in rows:
        status_of(row)
        refs = _evidence_refs(row)
        if not refs:
            raise DraftValidationError("CLAIM_EVIDENCE_REQUIRED")
        owner_historical = is_historical(row)
        for evidence_id in refs:
            _require_evidence(evidence, evidence_id, owner_historical=owner_historical)


def _pdf_hash(record: Mapping[str, Any]) -> str | None:
    for field in ("sha256", "artifact_sha256", "pdf_hash"):
        value = record.get(field)
        if value is not None:
            return validate_sha256(value, field=field)
    for field in ("descriptor", "pdf_descriptor", "artifact"):
        descriptor = record.get(field)
        if isinstance(descriptor, Mapping) and descriptor.get("sha256") is not None:
            return validate_sha256(descriptor.get("sha256"), field="pdf_sha256")
    return None


def _validate_block(
    block: Mapping[str, Any],
    *,
    claims: dict[str, dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
    owner_historical: bool,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    block_id = block.get("block_id") or block.get("id")
    validate_identifier(block_id, field="block_id")
    claim_refs = _block_refs(block, "claim_refs")
    evidence_refs = _block_refs(block, "evidence_refs")
    if not claim_refs and not evidence_refs:
        raise DraftValidationError("DRAFT_BLOCK_UNBOUND")
    for claim_id in claim_refs:
        if claim_id not in claims:
            raise DraftValidationError("DRAFT_CLAIM_NOT_FOUND")
    for evidence_id in evidence_refs:
        _require_evidence(evidence, evidence_id, owner_historical=owner_historical)
    _mapping(block.get("pdf_locator"), "DRAFT_PDF_LOCATOR_REQUIRED")
    if _pdf_hash(block) is None:
        raise DraftValidationError("DRAFT_PDF_DESCRIPTOR_REQUIRED")
    return claim_refs, evidence_refs


def _validate_drafts(
    rows: list[dict[str, Any]],
    *,
    claims: dict[str, dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
) -> None:
    for row in rows:
        status_of(row)
        owner_historical = is_historical(row)
        for claim_id in _claim_refs(row):
            if claim_id not in claims:
                raise DraftValidationError("DRAFT_CLAIM_NOT_FOUND")
        for evidence_id in _evidence_refs(row):
            _require_evidence(evidence, evidence_id, owner_historical=owner_historical)
        blocks = row.get("blocks", [])
        if not isinstance(blocks, (list, tuple)):
            raise DraftValidationError("DRAFT_BLOCKS_INVALID")
        seen_blocks: set[str] = set()
        for block in blocks:
            if not isinstance(block, Mapping):
                raise DraftValidationError("DRAFT_BLOCK_INVALID")
            block_id = block.get("block_id") or block.get("id")
            validate_identifier(block_id, field="block_id")
            if block_id in seen_blocks:
                raise DraftConflictError("DRAFT_BLOCK_DUPLICATE")
            seen_blocks.add(block_id)
            _validate_block(
                block,
                claims=claims,
                evidence=evidence,
                owner_historical=owner_historical,
            )


def _validate_pdfs(rows: list[dict[str, Any]], drafts: dict[str, dict[str, Any]]) -> None:
    for row in rows:
        status_of(row)
        draft_id = validate_identifier(row.get("draft_id"), field="draft_id")
        draft = drafts.get(draft_id)
        if draft is None:
            raise DraftValidationError("PDF_DRAFT_NOT_FOUND")
        if is_current(row) and is_historical(draft):
            raise DraftValidationError("PDF_DRAFT_STALE")
        if _pdf_hash(row) is None:
            raise DraftValidationError("PDF_DESCRIPTOR_REQUIRED")


def _records_dict(rows: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    return {node_identifier(kind, row): row for row in rows}


def _node_key(kind: str, record: Mapping[str, Any]) -> NodeKey:
    return kind, node_identifier(kind, record)


def _record_id(kind: str, record: dict[str, Any]) -> str:
    return node_identifier(kind, record)


def _set_record_id(kind: str, record: dict[str, Any], value: str) -> None:
    field = NODE_ID_FIELDS[kind]
    if field in record:
        record[field] = value
    elif "id" in record:
        record["id"] = value
    else:
        record[field] = value


class DraftWorkspace:
    """Immutable-in-use graph for one evidence-aware draft snapshot.

    Every mutation method returns a new workspace.  Read methods return deep
    copies, so inspecting or downloading a historical node cannot change the
    current pointer or any stored record.
    """

    def __init__(
        self,
        *,
        records: dict[str, dict[str, dict[str, Any]]],
        acceptance_layers: Mapping[str, str] | None = None,
    ) -> None:
        self._records = {
            kind: {
                identifier: copy_json(row)
                for identifier, row in records[kind].items()
            }
            for kind in NODE_KINDS
        }
        self._acceptance_layers = dict(
            acceptance_layers or DEFAULT_ACCEPTANCE_LAYERS
        )

    @classmethod
    def from_records(
        cls,
        *,
        evidence: object,
        claims: object,
        drafts: object,
        pdfs: object,
        acceptance_layers: Mapping[str, str] | None = None,
    ) -> "DraftWorkspace":
        evidence_rows = _rows(evidence, "evidence")
        claim_rows = _rows(claims, "claim")
        draft_rows = _rows(drafts, "draft")
        pdf_rows = _rows(pdfs, "pdf")
        _validate_evidence(evidence_rows)
        evidence_by_id = _records_dict(evidence_rows, "evidence")
        claim_by_id = _records_dict(claim_rows, "claim")
        _validate_claims(claim_rows, evidence_by_id)
        _validate_drafts(
            draft_rows,
            claims=claim_by_id,
            evidence=evidence_by_id,
        )
        draft_by_id = _records_dict(draft_rows, "draft")
        _validate_pdfs(pdf_rows, draft_by_id)
        layers = dict(DEFAULT_ACCEPTANCE_LAYERS)
        if acceptance_layers is not None:
            if set(acceptance_layers) != set(ACCEPTANCE_LAYERS):
                raise DraftValidationError("ACCEPTANCE_LAYERS_INVALID")
            for layer, value in acceptance_layers.items():
                if not isinstance(value, str) or not value.strip():
                    raise DraftValidationError("ACCEPTANCE_LAYER_STATUS_INVALID")
                layers[layer] = value
        return cls(
            records={
                "evidence": evidence_by_id,
                "claim": claim_by_id,
                "draft": draft_by_id,
                "pdf": _records_dict(pdf_rows, "pdf"),
            },
            acceptance_layers=layers,
        )

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> "DraftWorkspace":
        if not isinstance(snapshot, Mapping) or snapshot.get("schema_version") != SCHEMA_VERSION:
            raise DraftValidationError("SNAPSHOT_SCHEMA_INVALID")
        expected = snapshot.get("digest")
        base = {key: copy_json(snapshot[key]) for key in snapshot if key != "digest"}
        if expected != canonical_digest(base):
            raise DraftValidationError("SNAPSHOT_DIGEST_STALE")
        return cls.from_records(
            evidence=snapshot.get("evidence"),
            claims=snapshot.get("claims"),
            drafts=snapshot.get("drafts"),
            pdfs=snapshot.get("pdfs"),
            acceptance_layers=snapshot.get("acceptance_layers"),
        )

    def _rows(self, kind: str) -> list[dict[str, Any]]:
        if kind not in NODE_KINDS:
            raise DraftValidationError("NODE_KIND_INVALID")
        return [copy_json(self._records[kind][key]) for key in sorted(self._records[kind])]

    def _all_records(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "evidence": self._rows("evidence"),
            "claims": self._rows("claim"),
            "drafts": self._rows("draft"),
            "pdfs": self._rows("pdf"),
        }

    def _edges(self) -> list[dict[str, str]]:
        edges: list[dict[str, str]] = []
        for claim_id, claim in self._records["claim"].items():
            for evidence_id in _evidence_refs(claim):
                edges.append(
                    {
                        "from_kind": "evidence",
                        "from_id": evidence_id,
                        "to_kind": "claim",
                        "to_id": claim_id,
                        "relation": "evidence_supports_claim",
                    }
                )
        for draft_id, draft in self._records["draft"].items():
            claim_ids = set(_claim_refs(draft))
            evidence_ids = set(_evidence_refs(draft))
            for block in draft.get("blocks", []):
                if isinstance(block, Mapping):
                    claim_ids.update(_block_refs(block, "claim_refs"))
                    evidence_ids.update(_block_refs(block, "evidence_refs"))
            for claim_id in sorted(claim_ids):
                edges.append(
                    {
                        "from_kind": "claim",
                        "from_id": claim_id,
                        "to_kind": "draft",
                        "to_id": draft_id,
                        "relation": "claim_used_by_draft",
                    }
                )
            for evidence_id in sorted(evidence_ids):
                edges.append(
                    {
                        "from_kind": "evidence",
                        "from_id": evidence_id,
                        "to_kind": "draft",
                        "to_id": draft_id,
                        "relation": "evidence_cited_by_draft",
                    }
                )
        for pdf_id, pdf in self._records["pdf"].items():
            edges.append(
                {
                    "from_kind": "draft",
                    "from_id": pdf["draft_id"],
                    "to_kind": "pdf",
                    "to_id": pdf_id,
                    "relation": "draft_rendered_as_pdf_metadata",
                }
            )
        return sorted(edges, key=lambda row: tuple(row.values()))

    def _linkage(self) -> dict[str, Any]:
        edges = self._edges()
        divergent = any(is_divergent(row) for kind in NODE_KINDS for row in self._records[kind].values())
        for claim in self._records["claim"].values():
            parents = {
                lineage_id
                for evidence_id in _evidence_refs(claim)
                for lineage_id in lineage_ids(self._records["evidence"][evidence_id])
            }
            divergent = divergent or len(parents) > 1
        return {
            "evidence_refs": sorted(self._records["evidence"]),
            "claim_refs": sorted(self._records["claim"]),
            "draft_refs": sorted(self._records["draft"]),
            "pdf_refs": sorted(self._records["pdf"]),
            "edges": edges,
            "source_roles": sorted(
                {
                    role
                    for row in self._records["evidence"].values()
                    for role in source_roles(row)
                }
            ),
            "lineage_ids": sorted(
                {
                    lineage_id
                    for kind in NODE_KINDS
                    for row in self._records[kind].values()
                    for lineage_id in lineage_ids(row)
                }
            ),
            "divergent_lineage": divergent,
        }

    def snapshot(self) -> dict[str, Any]:
        base = {
            "schema_version": SCHEMA_VERSION,
            **self._all_records(),
            "linkage": self._linkage(),
            "acceptance_layers": dict(sorted(self._acceptance_layers.items())),
        }
        return {**copy_json(base), "digest": canonical_digest(base)}

    def _require_node(self, kind: str, node_id: str) -> dict[str, Any]:
        validate_identifier(node_id, field=f"{kind}_id")
        if kind not in NODE_KINDS:
            raise DraftValidationError("NODE_KIND_INVALID")
        try:
            return self._records[kind][node_id]
        except KeyError as exc:
            raise DraftValidationError("NODE_NOT_FOUND") from exc

    def view_node(self, kind: str, node_id: str) -> dict[str, Any]:
        return copy_json(self._require_node(kind, node_id))

    def current_ids(self, kind: str) -> list[str]:
        if kind not in NODE_KINDS:
            raise DraftValidationError("NODE_KIND_INVALID")
        return sorted(
            node_id
            for node_id, row in self._records[kind].items()
            if is_current(row)
        )

    def compare_nodes(self, kind: str, left_id: str, right_id: str) -> dict[str, Any]:
        left = self._require_node(kind, left_id)
        right = self._require_node(kind, right_id)
        fields = sorted(set(left) | set(right))
        changed = [field for field in fields if left.get(field) != right.get(field)]
        return {
            "schema_version": "review-writer.node-compare.v1",
            "kind": kind,
            "left_id": left_id,
            "right_id": right_id,
            "changed_fields": changed,
            "changes": {
                field: {
                    "left": copy_json(left.get(field)),
                    "right": copy_json(right.get(field)),
                }
                for field in changed
            },
        }

    def download_node(self, kind: str, node_id: str) -> DownloadArtifact:
        node = self._require_node(kind, node_id)
        payload = {
            "schema_version": "review-writer.node-download.v1",
            "kind": kind,
            "node": copy_json(node),
        }
        content = (
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        return DownloadArtifact(
            node_kind=kind,
            node_id=node_id,
            filename=f"{kind}-{node_id}.json",
            media_type="application/json",
            content=content,
        )

    def _adjacency(self) -> tuple[dict[NodeKey, set[NodeKey]], dict[NodeKey, set[NodeKey]]]:
        forward: dict[NodeKey, set[NodeKey]] = defaultdict(set)
        reverse: dict[NodeKey, set[NodeKey]] = defaultdict(set)
        for edge in self._edges():
            source = edge["from_kind"], edge["from_id"]
            target = edge["to_kind"], edge["to_id"]
            forward[source].add(target)
            reverse[target].add(source)
        return forward, reverse

    def _closure(self, target: NodeKey, *, downstream: bool) -> set[NodeKey]:
        forward, reverse = self._adjacency()
        neighbours = forward if downstream else reverse
        target_history = is_historical(self._records[target[0]][target[1]])
        result: set[NodeKey] = {target}
        queue: deque[NodeKey] = deque([target])
        while queue:
            current = queue.popleft()
            for candidate in neighbours.get(current, set()):
                candidate_row = self._records[candidate[0]][candidate[1]]
                if is_historical(candidate_row) != target_history:
                    continue
                if candidate not in result:
                    result.add(candidate)
                    queue.append(candidate)
        return result

    def impact_preview(
        self,
        kind: str,
        node_id: str,
        *,
        replacement: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = self._require_node(kind, node_id)
        target_key = kind, node_id
        affected = self._closure(target_key, downstream=True) | self._closure(
            target_key, downstream=False
        )
        downstream = self._closure(target_key, downstream=True) - {target_key}
        changed_fields: list[str] = []
        if replacement is not None:
            if not isinstance(replacement, Mapping):
                raise DraftValidationError("IMPACT_REPLACEMENT_INVALID")
            replacement_id = node_identifier(kind, replacement)
            if replacement_id != node_id:
                raise DraftConflictError("IMPACT_TARGET_ID_CHANGED")
            changed_fields = sorted(
                field
                for field in set(target) | set(replacement)
                if target.get(field) != replacement.get(field)
            )
        affected_by_kind = {
            node_kind: sorted(
                identifier
                for candidate_kind, identifier in affected
                if candidate_kind == node_kind
            )
            for node_kind in NODE_KINDS
        }
        downstream_by_kind = {
            node_kind: sorted(
                identifier
                for candidate_kind, identifier in downstream
                if candidate_kind == node_kind
            )
            for node_kind in NODE_KINDS
        }
        status_value = (
            replacement.get("status")
            if isinstance(replacement, Mapping) and "status" in replacement
            else target.get("status")
        )
        reasons: set[str] = set()
        target_status = status_code(status_value)
        if kind == "evidence" and target_status:
            reasons.add(f"EVIDENCE_STATUS_{target_status}")
        if is_historical(target):
            reasons.add("HISTORICAL_NODE_READ_ONLY")
        if self._linkage()["divergent_lineage"]:
            reasons.add("DIVERGENT_LINEAGE")
        return {
            "schema_version": IMPACT_PREVIEW_SCHEMA,
            "mode": "PREVIEW_ONLY",
            "mutation": "NONE",
            "promotion": "NONE",
            "target": {"kind": kind, "id": node_id},
            "changed_fields": changed_fields,
            "evidence_refs": affected_by_kind["evidence"],
            "claim_refs": affected_by_kind["claim"],
            "draft_refs": affected_by_kind["draft"],
            "pdf_refs": affected_by_kind["pdf"],
            "would_invalidate": [
                {"kind": node_kind, "id": identifier, "reason": "UPSTREAM_CHANGED"}
                for node_kind in NODE_KINDS
                for identifier in downstream_by_kind[node_kind]
            ],
            "blocking_reasons": sorted(reasons),
            "lineage": {
                "ids": self._linkage()["lineage_ids"],
                "divergent": self._linkage()["divergent_lineage"],
            },
            "acceptance_layers": dict(sorted(self._acceptance_layers.items())),
        }

    def _with_records(
        self,
        records: dict[str, dict[str, dict[str, Any]]],
    ) -> "DraftWorkspace":
        return type(self).from_records(
            evidence=list(records["evidence"].values()),
            claims=list(records["claim"].values()),
            drafts=list(records["draft"].values()),
            pdfs=list(records["pdf"].values()),
            acceptance_layers=self._acceptance_layers,
        )

    def branch_from_here(
        self,
        kind: str,
        node_id: str,
        *,
        branch_id: str,
    ) -> "DraftWorkspace":
        source = self._require_node(kind, node_id)
        if not is_historical(source):
            raise DraftBranchError("BRANCH_SOURCE_NOT_HISTORICAL")
        branch_id = validate_identifier(branch_id, field="branch_id")
        if branch_id in self._records[kind]:
            raise DraftConflictError("BRANCH_ID_EXISTS")
        branch = copy_json(source)
        _set_record_id(kind, branch, branch_id)
        branch["branch_from_id"] = node_id
        branch["current"] = False
        branch["historical"] = False
        branch["writable"] = False
        records = {
            node_kind: {
                identifier: copy_json(row)
                for identifier, row in rows.items()
            }
            for node_kind, rows in self._records.items()
        }
        records[kind][branch_id] = branch
        return self._with_records(records)

    def activate_branch(self, kind: str, branch_id: str) -> "DraftWorkspace":
        branch = self._require_node(kind, branch_id)
        if is_historical(branch) or is_current(branch) or not branch.get("branch_from_id"):
            raise DraftBranchError("BRANCH_NOT_PENDING")
        records = {
            node_kind: {
                identifier: copy_json(row)
                for identifier, row in rows.items()
            }
            for node_kind, rows in self._records.items()
        }
        demoted: set[NodeKey] = set()
        for identifier, row in records[kind].items():
            if is_current(row):
                row["current"] = False
                row["historical"] = True
                row["writable"] = False
                demoted.add((kind, identifier))
        records[kind][branch_id]["current"] = True
        records[kind][branch_id]["historical"] = False
        records[kind][branch_id]["writable"] = True
        forward, _ = self._adjacency()
        queue: deque[NodeKey] = deque(demoted)
        seen = set(demoted)
        while queue:
            current = queue.popleft()
            for candidate in forward.get(current, set()):
                if candidate in seen:
                    continue
                seen.add(candidate)
                row = records[candidate[0]][candidate[1]]
                if is_current(row):
                    row["current"] = False
                    row["historical"] = True
                    row["writable"] = False
                queue.append(candidate)
        return self._with_records(records)

    def replace_current(
        self,
        kind: str,
        node_id: str,
        replacement: Mapping[str, Any],
    ) -> "DraftWorkspace":
        current = self._require_node(kind, node_id)
        if is_historical(current) or not is_current(current) or not is_writable(current):
            raise DraftHistoryError()
        if not isinstance(replacement, Mapping):
            raise DraftValidationError("REPLACEMENT_INVALID")
        replacement_copy = copy_json(dict(replacement))
        if node_identifier(kind, replacement_copy) != node_id:
            raise DraftConflictError("REPLACEMENT_ID_CHANGED")
        if is_historical(replacement_copy) or not is_current(replacement_copy):
            raise DraftHistoryError("REPLACEMENT_HISTORY_MUTATION")
        records = {
            node_kind: {
                identifier: copy_json(row)
                for identifier, row in rows.items()
            }
            for node_kind, rows in self._records.items()
        }
        records[kind][node_id] = replacement_copy
        return self._with_records(records)


def build_impact_preview(
    workspace: DraftWorkspace,
    kind: str,
    node_id: str,
    *,
    replacement: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Functional alias for callers that prefer a command-style API."""

    return workspace.impact_preview(kind, node_id, replacement=replacement)


impact_preview = build_impact_preview
