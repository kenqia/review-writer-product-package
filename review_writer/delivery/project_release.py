"""Single-source manuscript editing and deterministic project release."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jsonschema import Draft202012Validator

from review_writer.delivery.figure_policy import (
    FigurePolicyError,
    figure_validation_is_current,
    validate_new_route_figure_policy,
    validate_figure_policy,
)
from review_writer.delivery.docx_integrity import (
    DocxIntegrityError,
    validate_docx_integrity,
)
from review_writer.delivery.chemical_paper_release import (
    ChemicalPaperReleaseError,
    analyze_chemical_paper_release,
    dependency_currentness_for_project,
    release_markdown_with_chemical_limitations,
    safe_chemical_paper_projection,
)
from review_writer.delivery.dual_parse_release import dual_parse_release_state
from review_writer.project.paper_evidence import (
    HONEST_PROGRESSIVE_ROUTE,
    PaperEvidenceError,
    honest_progressive_summary_from_projection,
)
from review_writer.project.manuscript_v2 import manuscript_state
from review_writer.project.source_truth import canonical_digest
from review_writer.project.synthesis import (
    SynthesisError,
    synthesis_state,
    validate_authoritative_review_questions as _validate_authoritative_review_questions,
)
from review_writer.project.vertical_review import VerticalReviewError, benchmark_metrics
from review_writer.project.workflow_projection import NEW_ROUTE, workflow_state


_ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_DOCX_CODE_FENCE_RE = re.compile(r"^```(\w*)\s*$")
_IMAGE_MARKER_RE = re.compile(r"!\[")
_CANONICAL_IMAGE_RE = re.compile(r"^!\[([^\]\r\n]*)\]\(([^\s()<>\"']+)\)[ \t]*$")
_REFERENCE_RE = re.compile(r"^\s*(?:\[(\d+)\]|(\d+)[.)])\s+\S")
_CITATION_RE = re.compile(r"(?<!!)\[([0-9][0-9,;\s\-–—]*)\]")
_CLAIM_MARKER_RE = re.compile(
    r"(?:\[claim:([A-Za-z0-9._:-]+)\]|<!--\s*claim(?:_id)?\s*:\s*([A-Za-z0-9._:-]+)\s*-->)",
    flags=re.IGNORECASE,
)
PROJECT_RELEASE_LOCK = threading.RLock()
RELEASE_LEVELS = frozenset({"SELF_REVIEWED_DRAFT", "EXPERT_REVIEWED_RELEASE"})
_SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas" / "delivery"


class ProjectReleaseError(ValueError):
    """The authoritative manuscript cannot be released safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def validate_authoritative_review_questions(value: object) -> dict[str, Any]:
    """Expose the synthesis question gate to release validation callers."""
    try:
        return _validate_authoritative_review_questions(value)
    except SynthesisError as exc:
        raise ProjectReleaseError(exc.code, "authoritative Review Questions are invalid") from exc


def _authoritative_review_question_binding(
    project: Path, lineage: object
) -> dict[str, Any] | None:
    lineage_authoritative = isinstance(lineage, dict) and lineage.get("authoritative_run") is True
    protocol_path = project / "02_synthesis/comparison_protocol.json"
    if not protocol_path.exists():
        if lineage_authoritative:
            raise ProjectReleaseError(
                "REVIEW_QUESTIONS_REQUIRED",
                "authoritative manuscript has no comparison protocol",
            )
        return None
    protocol = _read_json(protocol_path, "SYNTHESIS_PROTOCOL_INVALID")
    protocol_authoritative = isinstance(protocol, dict) and protocol.get("authoritative_run") is True
    if not (lineage_authoritative or protocol_authoritative):
        return None
    if not isinstance(protocol, dict):
        raise ProjectReleaseError("SYNTHESIS_PROTOCOL_INVALID", "comparison protocol is not an object")
    if lineage_authoritative and not protocol_authoritative:
        protocol = {**protocol, "authoritative_run": True}
    binding = validate_authoritative_review_questions(protocol)
    if isinstance(lineage, dict):
        for key in ("authoritative_run", "review_questions", "review_questions_digest"):
            if lineage.get(key) != binding.get(key):
                raise ProjectReleaseError(
                    "MANUSCRIPT_LINEAGE_STALE",
                    "authoritative Review Questions are not bound by manuscript lineage",
                )
    try:
        synthesis = synthesis_state(project)
    except (SynthesisError, PaperEvidenceError) as exc:
        raise ProjectReleaseError(exc.code, "authoritative synthesis question chain is invalid") from exc
    question_gate = synthesis.get("question_gate") if isinstance(synthesis, dict) else None
    if not isinstance(question_gate, dict) or question_gate.get("workflow_can_continue") is not True:
        code = (
            question_gate.get("reason_code")
            if isinstance(question_gate, dict)
            else "SYNTHESIS_REVIEW_QUESTION_MISSING"
        )
        raise ProjectReleaseError(
            str(code), "authoritative synthesis must cover five current Review Questions"
        )
    if synthesis.get("workflow_can_continue") is not True:
        raise ProjectReleaseError(
            str(synthesis.get("reason_code", "SYNTHESIS_NOT_APPROVED")),
            "authoritative synthesis is not current and dispositioned",
        )
    return binding


def honest_progressive_release_fields(summary: object) -> dict[str, Any]:
    """Return only the release-safe Honest Progressive fields."""

    try:
        normalized = honest_progressive_summary_from_projection(
            summary, project_scope=True
        )
    except PaperEvidenceError as exc:
        raise ProjectReleaseError(exc.code, "Honest Progressive summary is invalid") from exc
    if normalized is None:
        raise ProjectReleaseError(
            "HONEST_PROGRESSIVE_SUMMARY_INVALID",
            "Honest Progressive summary is missing",
        )
    return {
        "route": HONEST_PROGRESSIVE_ROUTE,
        "availability": normalized["availability"],
        "status": normalized["status"],
        "core_molecule_count": normalized["core_molecule_count"],
        "confirmed_count": normalized["confirmed_count"],
        "ai_provisional_count": normalized["ai_provisional_count"],
        "blocked_count": normalized["blocked_count"],
        "coverage_ratio": normalized["coverage_ratio"],
        "coverage_threshold": normalized["coverage_threshold"],
        "coverage_sufficient": normalized["coverage_sufficient"],
        "paper_coverage": normalized["paper_coverage"],
        "uncertainty_statement": normalized["uncertainty_statement"],
        "gap_registry": normalized["gap_registry"],
        "traceability": normalized["traceability"],
        "actor_provenance_residual": normalized["actor_provenance_residual"],
        "credits_status": "NOT_APPLICABLE_BY_CURRENT_SCOPE",
    }


def _section_id(heading: str, seen: dict[str, int]) -> str:
    base = re.sub(r"[^\w]+", "-", heading.casefold(), flags=re.UNICODE).strip("-_")
    base = base or "section"
    seen[base] = seen.get(base, 0) + 1
    return base if seen[base] == 1 else f"{base}-{seen[base]}"


def _split_manuscript_sections(markdown: str, *, include_spans: bool) -> list[dict[str, Any]]:
    if not isinstance(markdown, str):
        raise ProjectReleaseError("MANUSCRIPT_INVALID", "markdown must be text")

    matches: list[tuple[int, int, int, str]] = []
    offset = 0
    fence: str | None = None
    for line in markdown.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        fence_match = _FENCE_RE.match(content)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker[0]
            elif marker[0] == fence:
                fence = None
        elif fence is None:
            heading_match = _ATX_HEADING_RE.match(content)
            if heading_match:
                matches.append(
                    (
                        offset,
                        offset + len(line),
                        len(heading_match.group(1)),
                        heading_match.group(2).strip(),
                    )
                )
        offset += len(line)

    if not matches:
        raise ProjectReleaseError("MANUSCRIPT_SECTIONS_INVALID", "manuscript requires ATX headings")
    if markdown[: matches[0][0]].strip():
        raise ProjectReleaseError("MANUSCRIPT_SECTIONS_INVALID", "content before the first heading is not supported")

    seen: dict[str, int] = {}
    sections: list[dict[str, Any]] = []
    for index, (_, heading_end, level, heading) in enumerate(matches):
        body_end = matches[index + 1][0] if index + 1 < len(matches) else len(markdown)
        body = markdown[heading_end:body_end].strip("\r\n")
        section = {
            "id": _section_id(heading, seen),
            "heading": heading,
            "level": level,
            "body": body,
        }
        if include_spans:
            section["_body_start"] = heading_end
            section["_body_end"] = body_end
        sections.append(section)
    return sections


def split_manuscript_sections(markdown: str) -> list[dict[str, Any]]:
    """Split an ATX-heading manuscript into ordered, editable sections."""
    return _split_manuscript_sections(markdown, include_spans=False)


def replace_manuscript_section_body(markdown: str, section_id: str, body: str) -> str:
    """Replace one section body while preserving every non-target manuscript byte."""
    if not isinstance(section_id, str) or not section_id or not isinstance(body, str):
        raise ProjectReleaseError("MANUSCRIPT_SECTIONS_INVALID", "section_id and body are required")
    sections = _split_manuscript_sections(markdown, include_spans=True)
    targets = [section for section in sections if section["id"] == section_id]
    if len(targets) != 1:
        raise ProjectReleaseError("MANUSCRIPT_SECTIONS_INVALID", "target section must exist exactly once")
    target = targets[0]
    body_start = int(target["_body_start"])
    body_end = int(target["_body_end"])
    original_body = markdown[body_start:body_end]
    newline = "\r\n" if markdown[:body_start].endswith("\r\n") else "\n"
    normalized_body = body.strip("\r\n").replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)

    if not normalized_body:
        replacement = original_body if not original_body.strip("\r\n") else (
            original_body[: len(original_body) - len(original_body.lstrip("\r\n"))]
            + original_body[len(original_body.rstrip("\r\n")) :]
        )
    elif original_body.strip("\r\n"):
        leading_length = len(original_body) - len(original_body.lstrip("\r\n"))
        trailing_start = len(original_body.rstrip("\r\n"))
        replacement = original_body[:leading_length] + normalized_body + original_body[trailing_start:]
    else:
        replacement = newline + normalized_body
        replacement += newline * (2 if body_end < len(markdown) else 1)
    return markdown[:body_start] + replacement + markdown[body_end:]


def render_manuscript_sections(sections: list[dict[str, Any]]) -> str:
    """Render ordered section data back to one canonical Markdown manuscript."""
    if not isinstance(sections, list) or not sections:
        raise ProjectReleaseError("MANUSCRIPT_SECTIONS_INVALID", "sections must be a nonempty list")

    rendered: list[str] = []
    ids: list[str] = []
    headings: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            raise ProjectReleaseError("MANUSCRIPT_SECTIONS_INVALID", "section rows must be objects")
        section_id = section.get("id")
        heading = section.get("heading")
        level = section.get("level")
        body = section.get("body")
        if not isinstance(section_id, str) or not section_id.strip():
            raise ProjectReleaseError("MANUSCRIPT_SECTIONS_INVALID", "section ids must be nonempty")
        if not isinstance(heading, str) or not heading.strip() or "\n" in heading or "\r" in heading:
            raise ProjectReleaseError("MANUSCRIPT_SECTIONS_INVALID", "section headings must be single nonempty lines")
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 6:
            raise ProjectReleaseError("MANUSCRIPT_SECTIONS_INVALID", "section levels must be within 1..6")
        if not isinstance(body, str):
            raise ProjectReleaseError("MANUSCRIPT_SECTIONS_INVALID", "section bodies must be text")
        ids.append(section_id)
        headings.append(heading.strip().casefold())
        block = f"{'#' * level} {heading.strip()}"
        if body.strip("\r\n"):
            block += f"\n\n{body.strip(chr(13) + chr(10))}"
        rendered.append(block)

    if len(ids) != len(set(ids)) or len(headings) != len(set(headings)):
        raise ProjectReleaseError("MANUSCRIPT_SECTIONS_INVALID", "section ids and headings must be unique")
    return "\n\n".join(rendered)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha256(value: Any) -> str:
    try:
        return canonical_digest(value)
    except (TypeError, ValueError) as exc:
        raise ProjectReleaseError("RELEASE_STATE_INVALID", "release state must be finite JSON") from exc


def is_reparse_component(path: Path) -> bool:
    return path.is_symlink() or bool(hasattr(path, "is_junction") and path.is_junction())


def _reject_reparse_components(project: Path, relatives: tuple[Path, ...]) -> None:
    if is_reparse_component(project) or not project.is_dir():
        raise ProjectReleaseError("PROJECT_PATH_INVALID", "project root must be a real directory")
    for relative in relatives:
        component = project
        for part in relative.parts:
            component /= part
            if is_reparse_component(component):
                raise ProjectReleaseError("PROJECT_PATH_INVALID", "release path contains a symlink or reparse point")


def validate_project_path_components(project: Path, relatives: tuple[Path, ...]) -> None:
    """Reject symlink/reparse components across project-relative paths, including optional files."""
    _reject_reparse_components(Path(project), relatives)


def validate_project_file_path(project: Path, relative: Path, code: str) -> Path:
    """Return a required project file after rejecting symlink/reparse components."""
    validate_project_path_components(project, (relative,))
    candidate = project / relative
    if not candidate.is_file():
        raise ProjectReleaseError(code, "required release input is missing")
    try:
        candidate.resolve(strict=True).relative_to(project.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ProjectReleaseError("PROJECT_PATH_INVALID", "release input escapes the project") from exc
    return candidate


def _read_json(path: Path, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectReleaseError(code, "required JSON release state is missing or invalid") from exc


def _read_jsonl(path: Path, code: str) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectReleaseError(code, "required JSONL release state is missing or invalid") from exc
    if not all(isinstance(row, dict) for row in rows):
        raise ProjectReleaseError(code, "release JSONL must contain objects")
    return rows


def _without_docx_code_blocks(markdown: str) -> str:
    visible: list[str] = []
    in_code_block = False
    for line in markdown.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if not in_code_block and _DOCX_CODE_FENCE_RE.match(content):
            in_code_block = True
            visible.append("\n" if line.endswith(("\n", "\r")) else "")
        elif in_code_block:
            if content.startswith("```"):
                in_code_block = False
            visible.append("\n" if line.endswith(("\n", "\r")) else "")
        else:
            visible.append(line)
    return "".join(visible)


def _citation_numbers(markdown: str) -> set[int]:
    numbers: set[int] = set()
    for match in _CITATION_RE.finditer(markdown):
        for token in re.split(r"[,;\s]+", match.group(1).strip()):
            if not token:
                continue
            range_match = re.fullmatch(r"(\d+)[\-–—](\d+)", token)
            if range_match:
                start, end = map(int, range_match.groups())
                if start > end or end - start > 1000:
                    raise ProjectReleaseError("REFERENCES_INVALID", "citation range is invalid")
                numbers.update(range(start, end + 1))
            elif token.isdigit():
                numbers.add(int(token))
    return numbers


def _validate_references(sections: list[dict[str, Any]]) -> int:
    reference_sections = [section for section in sections if section["heading"].strip().casefold() == "references"]
    if len(reference_sections) != 1:
        raise ProjectReleaseError("REFERENCES_INVALID", "manuscript requires exactly one References section")
    body = reference_sections[0]["body"]
    if not body.strip():
        raise ProjectReleaseError("REFERENCES_INVALID", "References section must not be empty")
    reference_numbers: list[int] = []
    for line in body.splitlines():
        match = _REFERENCE_RE.match(line)
        if match:
            reference_numbers.append(int(match.group(1) or match.group(2)))
    if not reference_numbers or len(reference_numbers) != len(set(reference_numbers)):
        raise ProjectReleaseError("REFERENCES_INVALID", "references must have unique numeric entries")
    body_markdown = "\n\n".join(
        section["body"]
        for section in sections
        if section["heading"].strip().casefold() != "references"
    )
    missing = _citation_numbers(body_markdown) - set(reference_numbers)
    if missing:
        raise ProjectReleaseError("REFERENCES_INVALID", "manuscript cites a missing reference")
    return len(reference_numbers)


def _validated_image(project: Path, relative_url: str) -> Path:
    raw = relative_url.strip()
    parsed = urlparse(raw)
    if (
        not raw
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or raw.startswith(("/", "\\"))
        or "\\" in raw
    ):
        raise ProjectReleaseError("IMAGE_INVALID", "release images must use project-local relative paths")
    relative = Path(parsed.path)
    if any(part in {"", "."} for part in relative.parts):
        raise ProjectReleaseError("IMAGE_INVALID", "release image path is not canonical")

    project_root = project.resolve(strict=True)
    resolved_by_stage: list[Path] = []
    for stage in (project / "04_first_draft", project / "05_final_audit"):
        candidate = Path(os.path.normpath(os.fspath(stage / relative)))
        try:
            lexical_relative = candidate.relative_to(project)
        except ValueError as exc:
            raise ProjectReleaseError("IMAGE_INVALID", "release image is outside the project") from exc
        checked = project
        for part in lexical_relative.parts:
            checked /= part
            if is_reparse_component(checked):
                raise ProjectReleaseError("IMAGE_INVALID", "release image path contains a symlink or reparse point")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(project_root)
        except (OSError, ValueError) as exc:
            raise ProjectReleaseError("IMAGE_INVALID", "release image is missing or outside the project") from exc
        if not resolved.is_file():
            raise ProjectReleaseError("IMAGE_INVALID", "release image is not a regular file")
        resolved_by_stage.append(resolved)
    if resolved_by_stage[0] != resolved_by_stage[1]:
        raise ProjectReleaseError("IMAGE_INVALID", "image path does not bind draft and release to one source")
    return resolved_by_stage[0]


def manuscript_lineage_entries(lineage: dict[str, Any]) -> list[dict[str, Any]]:
    """Return claim-lineage rows across supported manuscript lineage layouts."""
    for key in ("claims", "claim_lineage", "manuscript_claims", "lineage"):
        value = lineage.get(key)
        if isinstance(value, list):
            return value
    sections = lineage.get("sections")
    entries: list[dict[str, Any]] = []
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            if isinstance(section.get("claims"), list):
                entries.extend(section["claims"])
            elif isinstance(section.get("claim_ids"), list):
                entries.extend(
                    {"claim_id": claim_id, "section_id": section.get("section_id") or section.get("id")}
                    for claim_id in section["claim_ids"]
                )
    return entries


def _pending_scientific_edits(lineage: dict[str, Any]) -> list[dict[str, Any]]:
    pending = lineage.get("pending_scientific_edits", [])
    if not isinstance(pending, list):
        raise ProjectReleaseError(
            "MANUSCRIPT_LINEAGE_INVALID",
            "pending_scientific_edits must be a list",
        )
    normalized: list[dict[str, Any]] = []
    section_ids: set[str] = set()
    for row in pending:
        if not isinstance(row, dict):
            raise ProjectReleaseError(
                "MANUSCRIPT_LINEAGE_INVALID",
                "pending_scientific_edits must contain objects",
            )
        section_id = row.get("section_id")
        verified_body = row.get("verified_body")
        reasons = row.get("reasons")
        if (
            not isinstance(section_id, str)
            or not section_id
            or section_id in section_ids
            or not isinstance(verified_body, str)
            or not isinstance(reasons, list)
            or not reasons
            or not all(isinstance(reason, str) and reason for reason in reasons)
        ):
            raise ProjectReleaseError(
                "MANUSCRIPT_LINEAGE_INVALID",
                "pending_scientific_edits require unique sections, verified text, and reasons",
            )
        section_ids.add(section_id)
        normalized.append(
            {
                "section_id": section_id,
                "verified_body": verified_body,
                "reasons": list(reasons),
            }
        )
    return normalized


def _literal_occurrence_count(text: str, needle: str) -> int:
    count = 0
    offset = 0
    while True:
        index = text.find(needle, offset)
        if index < 0:
            return count
        count += 1
        offset = index + 1


def _validate_manuscript_lineage(
    project: Path,
    markdown: str,
    *,
    lineage_override: dict[str, Any] | None = None,
    allow_pending_scientific_edits: bool = False,
    allow_pending_text_span_drift: bool = False,
) -> dict[str, Any]:
    project_path = Path(project)
    if not isinstance(markdown, str):
        raise ProjectReleaseError("MANUSCRIPT_INVALID", "markdown must be text")
    try:
        metrics = benchmark_metrics(project_path)
    except VerticalReviewError as exc:
        raise ProjectReleaseError(exc.code, "Task4 projection state is not release-ready") from exc

    projection_path = validate_project_file_path(
        project_path, Path("02_claims/claim_projection.jsonl"), "PROJECTION_INVALID"
    )
    writer_path = validate_project_file_path(
        project_path, Path("02_claims/writer_packet.json"), "WRITER_PACKET_INVALID"
    )
    lineage_path = None
    if lineage_override is None:
        lineage_path = validate_project_file_path(
            project_path,
            Path("04_first_draft/manuscript_lineage.json"),
            "MANUSCRIPT_LINEAGE_INVALID",
        )
    projection = _read_jsonl(projection_path, "PROJECTION_INVALID")
    writer_packet = _read_json(writer_path, "WRITER_PACKET_INVALID")
    lineage = lineage_override if lineage_override is not None else _read_json(
        lineage_path, "MANUSCRIPT_LINEAGE_INVALID"  # type: ignore[arg-type]
    )
    if not isinstance(writer_packet, dict) or not isinstance(lineage, dict):
        raise ProjectReleaseError("RELEASE_STATE_INVALID", "writer packet and lineage must be objects")
    pending = _pending_scientific_edits(lineage)
    if pending and not allow_pending_scientific_edits:
        raise ProjectReleaseError(
            "MANUSCRIPT_NEEDS_EVIDENCE_REVIEW",
            "pending scientific edits must be evidence-reviewed before release",
        )

    claim_ids = [row.get("claim_id") for row in projection]
    if any(not isinstance(claim_id, str) or not claim_id for claim_id in claim_ids) or len(claim_ids) != len(set(claim_ids)):
        raise ProjectReleaseError("PROJECTION_INVALID", "projection claim ids must be unique")
    projection_by_id = {row["claim_id"]: row for row in projection}
    projection_sha256 = _canonical_sha256(projection)
    if writer_packet.get("projection_sha256") != projection_sha256:
        raise ProjectReleaseError("WRITER_PACKET_STALE", "writer packet does not bind the current projection")
    approved_rows = [row for row in projection if row.get("decision") == "APPROVED"]
    packet_claims = writer_packet.get("claims")
    if not isinstance(packet_claims, list) or not all(isinstance(row, dict) for row in packet_claims):
        raise ProjectReleaseError("WRITER_PACKET_INVALID", "writer packet claims must be objects")
    if packet_claims != approved_rows:
        raise ProjectReleaseError("WRITER_PACKET_STALE", "writer packet whitelist differs from the current projection")
    whitelist = {row["claim_id"] for row in packet_claims}

    manuscript_sha256 = _sha256_bytes(markdown.encode("utf-8"))
    if lineage.get("manuscript_sha256") != manuscript_sha256:
        raise ProjectReleaseError("MANUSCRIPT_LINEAGE_DRIFT", "lineage does not bind the authoritative manuscript")
    if lineage.get("projection_sha256") != projection_sha256:
        raise ProjectReleaseError("MANUSCRIPT_LINEAGE_DRIFT", "lineage does not bind the current projection")

    sections = split_manuscript_sections(markdown)
    sections_by_id = {section["id"]: section for section in sections}
    section_ids = set(sections_by_id)
    pending_section_ids = {row["section_id"] for row in pending}
    if not pending_section_ids <= section_ids:
        raise ProjectReleaseError(
            "MANUSCRIPT_LINEAGE_DRIFT",
            "pending scientific edits reference an unknown manuscript section",
        )
    reference_count = _validate_references(sections)
    entries = manuscript_lineage_entries(lineage)
    if not all(isinstance(entry, dict) for entry in entries):
        raise ProjectReleaseError("MANUSCRIPT_LINEAGE_INVALID", "lineage claim entries must be objects")
    referenced: set[str] = set()
    for entry in entries:
        claim_id = entry.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id:
            raise ProjectReleaseError("MANUSCRIPT_LINEAGE_INVALID", "lineage entries require claim_id")
        projected = projection_by_id.get(claim_id)
        if projected is None or claim_id not in whitelist:
            raise ProjectReleaseError("CLAIM_NOT_WHITELISTED", "manuscript lineage references a claim outside the writer whitelist")
        if projected.get("decision") in {"BLOCKED", "HUMAN_REQUIRED"}:
            raise ProjectReleaseError("CLAIM_NOT_APPROVED", "blocked or human-required claims cannot enter the manuscript")
        if claim_id in referenced:
            raise ProjectReleaseError(
                "MANUSCRIPT_LINEAGE_DRIFT",
                "each lineage claim must appear exactly once",
            )
        section_id = entry.get("section_id")
        if section_id is not None and section_id not in section_ids:
            raise ProjectReleaseError("MANUSCRIPT_LINEAGE_DRIFT", "lineage references an unknown manuscript section")
        text_span = entry.get("text_span") or entry.get("manuscript_text") or entry.get("text")
        if text_span is not None:
            if not isinstance(text_span, str) or not text_span:
                raise ProjectReleaseError("MANUSCRIPT_LINEAGE_DRIFT", "lineage text span is absent from the manuscript")
            bound_text = sections_by_id[section_id]["body"] if section_id is not None else markdown
            span_may_drift = (
                allow_pending_text_span_drift
                and section_id is not None
                and section_id in pending_section_ids
            )
            if not span_may_drift and _literal_occurrence_count(bound_text, text_span) != 1:
                raise ProjectReleaseError(
                    "MANUSCRIPT_LINEAGE_DRIFT",
                    "lineage text span must occur exactly once in its bound manuscript section",
                )
        referenced.add(claim_id)

    marker_ids = [match.group(1) or match.group(2) for match in _CLAIM_MARKER_RE.finditer(markdown)]
    if set(marker_ids) != referenced or len(marker_ids) != len(set(marker_ids)):
        raise ProjectReleaseError(
            "MANUSCRIPT_LINEAGE_DRIFT",
            "manuscript claim markers and lineage claims must match one-to-one",
        )

    visible_markdown = _without_docx_code_blocks(markdown)
    image_paths: list[str] = []
    for line in visible_markdown.splitlines():
        if not _IMAGE_MARKER_RE.search(line):
            continue
        image_match = _CANONICAL_IMAGE_RE.fullmatch(line)
        if image_match is None:
            raise ProjectReleaseError(
                "IMAGE_INVALID",
                "release images must use standalone ![alt](project-relative-path) syntax without titles",
            )
        image_paths.append(image_match.group(2))
    for image_path in image_paths:
        _validated_image(project_path, image_path)

    return {
        "status": "valid",
        "project_id": metrics["project_id"],
        "manuscript_sha256": manuscript_sha256,
        "projection_sha256": projection_sha256,
        "claim_reference_count": len(referenced),
        "approved_claim_ids": sorted(whitelist),
        "reference_count": reference_count,
        "image_count": len(image_paths),
        "image_paths": image_paths,
    }


def validate_manuscript_lineage(project: Path, markdown: str) -> dict[str, Any]:
    """Validate current Task4 state, manuscript lineage, citations, and images."""
    return _validate_manuscript_lineage(project, markdown)


def bind_authoritative_draft(
    project: Path,
    manuscript_input: Path,
    lineage_input: Path,
) -> dict[str, Any]:
    """Validate and bind exact provider outputs to the one canonical draft location."""
    project_path = Path(project).resolve()
    try:
        manuscript_bytes = Path(manuscript_input).read_bytes()
        lineage_bytes = Path(lineage_input).read_bytes()
        manuscript = manuscript_bytes.decode("utf-8")
        lineage = json.loads(lineage_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectReleaseError(
            "DRAFT_BIND_INPUT_INVALID",
            "provider manuscript and lineage must be readable UTF-8 files",
        ) from exc
    if not isinstance(lineage, dict):
        raise ProjectReleaseError("DRAFT_BIND_INPUT_INVALID", "provider lineage must be an object")
    validation = _validate_manuscript_lineage(
        project_path,
        manuscript,
        lineage_override=lineage,
    )
    manuscript_path = project_path / "04_first_draft" / "first_draft.md"
    lineage_path = project_path / "04_first_draft" / "manuscript_lineage.json"
    for destination, payload in (
        (manuscript_path, manuscript_bytes),
        (lineage_path, lineage_bytes),
    ):
        if destination.exists() and destination.read_bytes() != payload:
            raise ProjectReleaseError(
                "DRAFT_BIND_CONFLICT",
                "canonical draft already exists with different bytes",
            )
    _atomic_write(manuscript_path, manuscript_bytes)
    _atomic_write(lineage_path, lineage_bytes)
    state_path = project_path / "00_brief" / "review_state.json"
    state = _read_json(state_path, "PROJECT_STATE_INVALID")
    if not isinstance(state, dict):
        raise ProjectReleaseError("PROJECT_STATE_INVALID", "review state must be an object")
    updated_state = {**state, "current_stage": "drafting", "status": "in_progress"}
    _atomic_write(state_path, _json_bytes(updated_state))
    return {
        "claim_reference_count": validation["claim_reference_count"],
        "image_count": validation["image_count"],
        "project_id": validation["project_id"],
    }


def validated_draft_manuscript_lineage(project: Path, markdown: str) -> dict[str, Any]:
    """Read and validate authoritative lineage for the researcher draft view.

    Pending scientific edits may temporarily invalidate text-span placement only in
    their bound sections. Manuscript/projection hashes, writer-packet binding, the
    claim whitelist, references, section bindings, and images remain mandatory.
    """
    project_path = Path(project)
    lineage_path = validate_project_file_path(
        project_path,
        Path("04_first_draft/manuscript_lineage.json"),
        "MANUSCRIPT_LINEAGE_INVALID",
    )
    lineage = _read_json(lineage_path, "MANUSCRIPT_LINEAGE_INVALID")
    if not isinstance(lineage, dict):
        raise ProjectReleaseError("MANUSCRIPT_LINEAGE_INVALID", "lineage must be an object")
    pending = _pending_scientific_edits(lineage)
    if not pending:
        validate_manuscript_lineage(project_path, markdown)
        return lineage
    _validate_manuscript_lineage(
        project_path,
        markdown,
        lineage_override=lineage,
        allow_pending_scientific_edits=True,
        allow_pending_text_span_drift=True,
    )
    return lineage


def refreshed_manuscript_lineage(
    project: Path,
    current_markdown: str,
    candidate_markdown: str,
    *,
    section_id: str | None = None,
    scientific_reasons: list[str] | None = None,
) -> dict[str, Any]:
    """Refresh lineage, retaining scientific edits as release-blocking pending revisions."""
    project_path = Path(project)
    lineage_path = validate_project_file_path(
        project_path, Path("04_first_draft/manuscript_lineage.json"), "MANUSCRIPT_LINEAGE_INVALID"
    )
    lineage = _read_json(lineage_path, "MANUSCRIPT_LINEAGE_INVALID")
    if not isinstance(lineage, dict):
        raise ProjectReleaseError("MANUSCRIPT_LINEAGE_INVALID", "lineage must be an object")
    pending = _pending_scientific_edits(lineage)
    current_sha256 = _sha256_bytes(current_markdown.encode("utf-8"))
    if lineage.get("manuscript_sha256") != current_sha256:
        raise ProjectReleaseError(
            "MANUSCRIPT_LINEAGE_DRIFT",
            "lineage does not bind the current authoritative manuscript",
        )
    if not pending:
        validate_manuscript_lineage(project_path, current_markdown)

    updated = dict(lineage)
    updated["manuscript_sha256"] = _sha256_bytes(candidate_markdown.encode("utf-8"))
    reasons = list(dict.fromkeys(scientific_reasons or []))
    if any(not isinstance(reason, str) or not reason for reason in reasons):
        raise ProjectReleaseError(
            "MANUSCRIPT_LINEAGE_INVALID",
            "scientific edit reasons must be nonempty text",
        )
    if reasons and not section_id:
        raise ProjectReleaseError(
            "MANUSCRIPT_LINEAGE_INVALID",
            "scientific edits require a manuscript section",
        )

    touched_pending = False
    if section_id is not None:
        current_sections = {row["id"]: row for row in split_manuscript_sections(current_markdown)}
        candidate_sections = {row["id"]: row for row in split_manuscript_sections(candidate_markdown)}
        if section_id not in current_sections or section_id not in candidate_sections:
            raise ProjectReleaseError(
                "MANUSCRIPT_SECTIONS_INVALID",
                "scientific edit section is missing",
            )
        existing = next((row for row in pending if row["section_id"] == section_id), None)
        candidate_body = candidate_sections[section_id]["body"]
        if existing is not None and candidate_body == existing["verified_body"]:
            pending = [row for row in pending if row["section_id"] != section_id]
            touched_pending = True
        elif reasons:
            if existing is None:
                pending.append(
                    {
                        "section_id": section_id,
                        "verified_body": current_sections[section_id]["body"],
                        "reasons": reasons,
                    }
                )
            else:
                existing["reasons"] = list(dict.fromkeys([*existing["reasons"], *reasons]))
            touched_pending = True

    pending.sort(key=lambda row: row["section_id"])
    if pending:
        updated["pending_scientific_edits"] = pending
    elif "pending_scientific_edits" in lineage or touched_pending:
        updated["pending_scientific_edits"] = []
    else:
        updated.pop("pending_scientific_edits", None)
    if not pending:
        _validate_manuscript_lineage(project_path, candidate_markdown, lineage_override=updated)
    return updated


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _json_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProjectReleaseError("QUALITY_REPORT_INVALID", "quality report must be finite JSON") from exc


def _validate_release_schema(value: Any, filename: str) -> None:
    try:
        schema = json.loads((_SCHEMA_ROOT / filename).read_text(encoding="utf-8"))
        errors = sorted(
            Draft202012Validator(schema).iter_errors(value),
            key=lambda error: list(error.path),
        )
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise ProjectReleaseError(
            "RELEASE_SCHEMA_INVALID", "release schema is unavailable or invalid"
        ) from exc
    if errors:
        raise ProjectReleaseError(
            "RELEASE_SCHEMA_INVALID", "release payload does not match its schema"
        )


def _release_status(project: Path) -> str:
    state = _read_json(project / "00_brief" / "review_state.json", "PROJECT_STATE_INVALID")
    candidates = []
    if isinstance(state, dict):
        candidates.extend((state.get("release_status"), state.get("review_status"), state.get("status")))
        brief = state.get("brief")
        if isinstance(brief, dict):
            candidates.extend((brief.get("release_status"), brief.get("review_status"), brief.get("status")))
    return "DOMAIN_EXPERT_REVIEWED" if "DOMAIN_EXPERT_REVIEWED" in candidates else "AI_REVIEWED_BENCHMARK"


def _validate_docx_attributions(docx_path: Path, figure_validation: dict[str, Any]) -> None:
    required = [
        row["attribution"]
        for row in figure_validation.get("figures", [])
        if isinstance(row, dict)
        and isinstance(row.get("attribution"), str)
        and row["attribution"].strip()
    ]
    try:
        with zipfile.ZipFile(docx_path) as archive:
            names = set(archive.namelist())
            required_parts = {"[Content_Types].xml", "word/document.xml"}
            if not required_parts <= names:
                raise ProjectReleaseError(
                    "DOCX_EXPORT_FAILED",
                    "DOCX converter output is missing required document parts",
                )
            ET.fromstring(archive.read("[Content_Types].xml"))
            text_parts = ["".join(ET.fromstring(archive.read("word/document.xml")).itertext())]
            for name in ("word/footnotes.xml", "word/endnotes.xml"):
                if name in names:
                    text_parts.append("".join(ET.fromstring(archive.read(name)).itertext()))
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise ProjectReleaseError("DOCX_EXPORT_FAILED", "DOCX converter output is invalid") from exc
    document_text = " ".join("\n".join(text_parts).split())
    if any(" ".join(attribution.split()) not in document_text for attribution in required):
        raise ProjectReleaseError(
            "DOCX_ATTRIBUTION_MISSING",
            "released DOCX must include every required source attribution",
        )


def _release_figure_validation(
    project: Path,
    *,
    approved_claim_ids: list[str],
    manuscript_sha256: str,
    manuscript_image_paths: list[str],
    manuscript_markdown: str,
) -> dict[str, Any]:
    manifest_relative = Path("03_figure_redraw/figure_manifest.json")
    validate_project_path_components(project, (manifest_relative,))
    manifest_path = project / manifest_relative
    if manifest_path.is_file():
        manifest = _read_json(manifest_path, "FIGURE_POLICY_INVALID")
    else:
        manifest = {"schema_version": "review-writer-figure-manifest.v1", "figures": []}
    image_files_by_markdown_path = {
        image_path: _validated_image(project, image_path)
        for image_path in manuscript_image_paths
    }
    try:
        return validate_figure_policy(
            manifest,
            approved_claim_ids=approved_claim_ids,
            manuscript_sha256=manuscript_sha256,
            manuscript_image_paths=manuscript_image_paths,
            manuscript_markdown=manuscript_markdown,
            image_files_by_markdown_path=image_files_by_markdown_path,
        )
    except FigurePolicyError as exc:
        raise ProjectReleaseError(exc.code, str(exc).split(": ", 1)[-1]) from exc


def project_figure_validation_is_current(
    project: Path,
    validation: Any,
    *,
    manuscript_sha256: str,
) -> bool:
    """Check whether stored figure validation still binds current manifest and image bytes."""
    project_path = Path(project)
    manifest_relative = Path("03_figure_redraw/figure_manifest.json")
    try:
        manifest_path = validate_project_file_path(
            project_path,
            manifest_relative,
            "FIGURE_POLICY_INVALID",
        )
        manifest = _read_json(manifest_path, "FIGURE_POLICY_INVALID")
        raw_figures = manifest.get("figures") if isinstance(manifest, dict) else None
        if not isinstance(raw_figures, list):
            return False
        image_files: dict[str, Path] = {}
        for row in raw_figures:
            if not isinstance(row, dict) or row.get("figure_type") == "FIGURE_BRIEF_PLACEHOLDER":
                return False
            markdown_path = row.get("markdown_path")
            if not isinstance(markdown_path, str) or not markdown_path:
                return False
            image_files[markdown_path] = _validated_image(project_path, markdown_path)
    except (OSError, ProjectReleaseError):
        return False
    return figure_validation_is_current(
        validation,
        manuscript_sha256=manuscript_sha256,
        manifest=manifest,
        image_files_by_markdown_path=image_files,
    )


def _restore_release(
    paths: tuple[Path, ...], previous: dict[Path, bytes | None]
) -> None:
    for path in paths:
        payload = previous[path]
        if payload is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write(path, payload)


def _stage_release_file(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        staged = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return staged


def _commit_release_files(staged_by_target: dict[Path, Path]) -> None:
    """Commit a validated release set, restoring original inodes on commit failure."""
    backups: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for target in staged_by_target:
            if not target.exists():
                continue
            with tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".backup",
                delete=False,
            ) as handle:
                backup = Path(handle.name)
            backup.unlink()
            os.link(target, backup)
            backups[target] = backup
        for target, staged in staged_by_target.items():
            os.replace(staged, target)
            replaced.append(target)
    except Exception:
        for target in reversed(replaced):
            backup = backups.pop(target, None)
            if backup is None:
                target.unlink(missing_ok=True)
            else:
                os.replace(backup, target)
        raise
    finally:
        for staged in staged_by_target.values():
            staged.unlink(missing_ok=True)
        for backup in backups.values():
            backup.unlink(missing_ok=True)


def _new_route_image_paths(markdown: str) -> list[str]:
    paths: list[str] = []
    fenced: str | None = None
    for line in markdown.splitlines():
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)[0]
            fenced = None if fenced == marker else marker
            continue
        if fenced is not None or not _IMAGE_MARKER_RE.search(line):
            continue
        match = _CANONICAL_IMAGE_RE.fullmatch(line)
        if match is None:
            raise ProjectReleaseError("IMAGE_INVALID", "new-route images must use standalone canonical Markdown")
        path = match.group(2)
        parsed = urlparse(path)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or path.startswith(("/", "\\"))
            or "\\" in path
        ):
            raise ProjectReleaseError("IMAGE_INVALID", "new-route images must be project-local")
        paths.append(path)
    if len(paths) != len(set(paths)):
        raise ProjectReleaseError("IMAGE_INVALID", "new-route image paths must be unique")
    return paths


def _new_route_figure_state(
    project: Path,
    markdown: str,
    *,
    release_level: str,
    lineage_digest: str,
) -> dict[str, Any]:
    registry_path = validate_project_file_path(
        project, Path("03_figures/source_figure_registry.json"), "FIGURE_POLICY_INVALID"
    )
    placeholder_relative = Path("03_figures/synthesis_figure_placeholders.json")
    # A project with only selected source figures has no synthesis placeholder
    # state.  Missing state means an empty placeholder set; malformed or
    # symlinked state remains fail-closed through the normal path validator.
    placeholder_path = (
        validate_project_file_path(project, placeholder_relative, "FIGURE_POLICY_INVALID")
        if os.path.lexists(project / placeholder_relative)
        else None
    )
    registry = _read_json(registry_path, "FIGURE_POLICY_INVALID")
    placeholder_state = (
        _read_json(placeholder_path, "FIGURE_POLICY_INVALID")
        if placeholder_path is not None
        else {"placeholders": []}
    )
    placeholders = (
        placeholder_state.get("placeholders")
        if isinstance(placeholder_state, dict)
        else None
    )
    try:
        return validate_new_route_figure_policy(
            project,
            source_registry=registry,
            placeholders=placeholders,
            manuscript_markdown=markdown,
            manuscript_image_paths=_new_route_image_paths(markdown),
            release_level=release_level,
            lineage_digest=lineage_digest,
        )
    except FigurePolicyError as exc:
        raise ProjectReleaseError(exc.code, str(exc).split(": ", 1)[-1]) from exc


def _new_route_release_paths(project: Path, release_level: str) -> tuple[Path, Path, Path, Path]:
    base = (
        "self_reviewed_draft"
        if release_level == "SELF_REVIEWED_DRAFT"
        else "expert_reviewed_release"
    )
    stage = project / "05_release"
    return (
        stage / f"{base}.md",
        stage / f"{base}.docx",
        stage / "release_snapshot.json",
        stage / "quality_report.json",
    )


def _chemical_paper_release_state(
    project: Path, lineage: dict[str, Any]
) -> dict[str, Any]:
    try:
        state = analyze_chemical_paper_release(lineage)
        currentness = dependency_currentness_for_project(project, state)
    except ChemicalPaperReleaseError as exc:
        raise ProjectReleaseError(exc.code, "Chemical Paper lineage is invalid or stale") from exc
    if (
        state["status"] == "available"
        and currentness["lineage_binding_status"] != "current"
    ):
        raise ProjectReleaseError(
            "CHEMICAL_PAPER_LINEAGE_STALE",
            "Chemical Paper authority binding is not current",
        )
    state["dependency_currentness"] = currentness
    if (
        state["status"] == "available"
        and not currentness["can_release"]
        and "CHEMICAL_DEPENDENCY_UNRESOLVED" not in state["issues"]
    ):
        state["issues"] = sorted(
            [*state["issues"], "CHEMICAL_DEPENDENCY_UNRESOLVED"]
        )
    return state


def _new_route_release(
    project: Path,
    workflow: dict[str, Any],
    *,
    release_level: str,
    python_executable: Path,
) -> dict[str, Any]:
    if release_level not in RELEASE_LEVELS:
        raise ProjectReleaseError("RELEASE_LEVEL_INVALID", "release level is unsupported")
    if not workflow.get("parse_ready"):
        raise ProjectReleaseError(
            "PARSE_QUALITY_NOT_READY", "source-truth parse review must close before release"
        )
    # Run the question-specific gate before the aggregate workflow readiness
    # check so a missing, duplicate, stale, or undispositioned question is
    # reported precisely.
    _authoritative_review_question_binding(project, None)
    if not workflow.get("internal_draft_export_ready"):
        raise ProjectReleaseError(
            "REVIEW_WORKFLOW_NOT_READY", "evidence-to-release review must close before release"
        )
    authoritative = manuscript_state(project)
    if not isinstance(authoritative, dict) or authoritative.get("workflow_can_continue") is not True:
        reason = authoritative.get("reason_code") if isinstance(authoritative, dict) else "MANUSCRIPT_INVALID"
        raise ProjectReleaseError("MANUSCRIPT_NOT_APPROVED", str(reason))
    source = validate_project_file_path(
        project, Path("04_manuscript/manuscript.md"), "MANUSCRIPT_INVALID"
    )
    lineage_path = validate_project_file_path(
        project,
        Path("04_manuscript/manuscript_lineage.v2.json"),
        "MANUSCRIPT_LINEAGE_INVALID",
    )
    try:
        manuscript_bytes = source.read_bytes()
        markdown = manuscript_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProjectReleaseError("MANUSCRIPT_INVALID", "authoritative manuscript must be UTF-8") from exc
    lineage = _read_json(lineage_path, "MANUSCRIPT_LINEAGE_INVALID")
    _authoritative_review_question_binding(project, lineage)
    workflow_digest = workflow.get("workflow_digest")
    lineage_digest = lineage.get("lineage_digest") if isinstance(lineage, dict) else None
    manuscript_sha256 = _sha256_bytes(manuscript_bytes)
    if (
        not isinstance(workflow_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", workflow_digest)
        or not isinstance(lineage_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", lineage_digest)
        or authoritative.get("manuscript_sha256") != manuscript_sha256
        or authoritative.get("lineage_digest") != lineage_digest
    ):
        raise ProjectReleaseError("MANUSCRIPT_LINEAGE_STALE", "authoritative manuscript binding is stale")
    dual_quality: dict[str, Any] = {}
    honest_summary: dict[str, Any] | None = None
    dual_source_root = project / "01_evidence/dual_source"
    has_dual_lineage = isinstance(lineage, dict) and isinstance(
        lineage.get("dual_parse_bindings"), list
    )
    if dual_source_root.exists() or has_dual_lineage:
        if (
            dual_source_root.is_symlink()
            or not dual_source_root.is_dir()
            or not has_dual_lineage
        ):
            raise ProjectReleaseError(
                "DUAL_PARSE_STALE", "dual-parse manuscript authority is missing or stale"
            )
        dual_state = dual_parse_release_state(project)
        try:
            honest_summary = honest_progressive_summary_from_projection(
                dual_state, project_scope=True
            )
        except PaperEvidenceError as exc:
            raise ProjectReleaseError(
                exc.code, "Honest Progressive release projection is invalid"
            ) from exc
        hard_fails = dual_state.get("hard_fails")
        if (
            dual_state.get("internal_release_ready") is not True
            or not isinstance(hard_fails, list)
            or hard_fails
        ):
            code = (
                hard_fails[0]
                if isinstance(hard_fails, list)
                and hard_fails
                and isinstance(hard_fails[0], str)
                else "DUAL_PARSE_STALE"
            )
            raise ProjectReleaseError(code, "dual-parse release authority is not current")
        if (
            dual_state.get("dual_parse_status") != "current"
            or dual_state.get("reaction_data_status")
            not in {"available", "unavailable_not_provided"}
            or dual_state.get("credits_status")
            != "NOT_APPLICABLE_BY_CURRENT_SCOPE"
        ):
            raise ProjectReleaseError(
                "DUAL_PARSE_STALE", "dual-parse release projection is invalid"
            )
        dual_quality = {
            "dual_parse_status": "current",
            "dual_parse_binding_digest": canonical_digest(
                lineage["dual_parse_bindings"]
            ),
            "reaction_data_status": dual_state["reaction_data_status"],
            "reaction_count": dual_state.get("reaction_count"),
            "credits_status": "NOT_APPLICABLE_BY_CURRENT_SCOPE",
        }
        if honest_summary is not None:
            dual_quality.update(honest_progressive_release_fields(honest_summary))
    if honest_summary is None:
        candidate = workflow.get("honest_progressive")
        if candidate is None:
            candidate = workflow.get("honest_progressive_summary")
        if candidate is not None:
            try:
                honest_summary = honest_progressive_summary_from_projection(
                    candidate, project_scope=True
                )
            except PaperEvidenceError as exc:
                raise ProjectReleaseError(
                    exc.code, "Honest Progressive release projection is invalid"
                ) from exc
            if honest_summary is not None:
                dual_quality.update(honest_progressive_release_fields(honest_summary))
    chemical_paper = _chemical_paper_release_state(project, lineage)
    if (
        release_level == "EXPERT_REVIEWED_RELEASE"
        and not chemical_paper["dependency_currentness"]["can_release"]
    ):
        raise ProjectReleaseError(
            "CHEMICAL_DEPENDENCY_UNRESOLVED",
            "expert release depends on unresolved or unreviewed chemical fields",
        )
    try:
        release_markdown = release_markdown_with_chemical_limitations(
            markdown, chemical_paper
        )
    except ChemicalPaperReleaseError as exc:
        raise ProjectReleaseError(exc.code, "Chemical Paper limitation lineage is ambiguous") from exc
    release_markdown_bytes = release_markdown.encode("utf-8")
    release_markdown_sha256 = _sha256_bytes(release_markdown_bytes)
    chemical_paper_safe = safe_chemical_paper_projection(chemical_paper)
    figure_validation = _new_route_figure_state(
        project,
        markdown,
        release_level=release_level,
        lineage_digest=lineage_digest,
    )
    snapshot, docx, release_snapshot, quality = _new_route_release_paths(project, release_level)
    release_paths = (snapshot, docx, release_snapshot, quality)
    validate_project_path_components(
        project, tuple(path.relative_to(project) for path in release_paths)
    )
    converter = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "review-export-docx"
        / "scripts"
        / "md2docx.py"
    )
    staged: dict[Path, Path] = {}
    temporary_docx: Path | None = None
    try:
        if not converter.is_file():
            raise ProjectReleaseError("DOCX_CONVERTER_MISSING", "repository DOCX converter is unavailable")
        staged[snapshot] = _stage_release_file(snapshot, release_markdown_bytes)
        with tempfile.NamedTemporaryFile(
            dir=snapshot.parent,
            prefix=f".{docx.stem}.",
            suffix=".docx.tmp",
            delete=False,
        ) as handle:
            temporary_docx = Path(handle.name)
        temporary_docx.unlink()
        try:
            completed = subprocess.run(
                [
                    str(Path(python_executable)),
                    str(converter),
                    "--input",
                    str(staged[snapshot]),
                    "--output",
                    str(temporary_docx),
                    "--project-id",
                    project.name,
                    "--release-level",
                    release_level,
                ],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProjectReleaseError("DOCX_EXPORT_FAILED", "DOCX converter could not complete") from exc
        if completed.returncode != 0 or not temporary_docx.is_file() or temporary_docx.stat().st_size <= 0:
            raise ProjectReleaseError("DOCX_EXPORT_FAILED", "DOCX converter did not produce a document")

        legacy_docx = project / "05_final_audit/final_draft.docx"
        try:
            integrity = validate_docx_integrity(
                temporary_docx,
                markdown=release_markdown,
                expected_media_sha256=figure_validation["expected_media_sha256"],
                required_attributions=figure_validation["required_attributions"],
                workflow_digest=workflow_digest,
                snapshot_workflow_digest=workflow_digest,
                legacy_docx=legacy_docx if legacy_docx.is_file() and not legacy_docx.is_symlink() else None,
                expected_project_id=project.name,
                expected_release_level=release_level,
            )
        except DocxIntegrityError as exc:
            raise ProjectReleaseError(exc.code, str(exc).split(": ", 1)[-1]) from exc

        if release_level == "EXPERT_REVIEWED_RELEASE":
            from review_writer.evaluation.review_benchmark import verified_synthesis_figures_current

            if not verified_synthesis_figures_current(
                project,
                lineage_digest=lineage_digest,
                manuscript_text=markdown,
                docx_path=temporary_docx,
            ):
                raise ProjectReleaseError(
                    "FIGURE_PLACEHOLDER_PENDING",
                    "expert release requires current human figure verification",
                )

        docx_bytes = temporary_docx.read_bytes()
        docx_sha256 = _sha256_bytes(docx_bytes)
        honest_fields = (
            honest_progressive_release_fields(honest_summary)
            if honest_summary is not None
            else {}
        )
        snapshot_payload = {
            "schema_version": "release-snapshot.v1",
            "project_id": project.name,
            "route": honest_fields.get("route", NEW_ROUTE),
            "release_level": release_level,
            "status": release_level,
            "workflow_digest": workflow_digest,
            "lineage_digest": lineage_digest,
            "manuscript_sha256": manuscript_sha256,
            "release_markdown_sha256": release_markdown_sha256,
            "chemical_paper_binding_digest": chemical_paper["binding_digest"],
            "chemical_paper_safe_summary": chemical_paper_safe,
            "chemical_paper_dependency_can_release": chemical_paper[
                "dependency_currentness"
            ]["can_release"],
            "markdown_path": snapshot.relative_to(project).as_posix(),
            "docx_path": docx.relative_to(project).as_posix(),
            "docx_sha256": docx_sha256,
            "placeholder_count": figure_validation["placeholder_count"],
            "pending_placeholder_count": figure_validation["pending_placeholder_count"],
            "hard_fail_signals": [],
            "system_generated_synthesis_figure": False,
            "integrity": integrity,
        }
        snapshot_payload.update(honest_fields)
        quality_payload = {
            "schema_version": "project-release.v2",
            **honest_fields,
            "status": release_level,
            "release_level": release_level,
            "workflow_digest": workflow_digest,
            "lineage_digest": lineage_digest,
            "manuscript_sha256": manuscript_sha256,
            "release_markdown_sha256": release_markdown_sha256,
            "chemical_paper_binding_digest": chemical_paper["binding_digest"],
            "chemical_paper_safe_summary": chemical_paper_safe,
            "chemical_paper_dependency_can_release": chemical_paper[
                "dependency_currentness"
            ]["can_release"],
            "docx_sha256": docx_sha256,
            "figure_validation": figure_validation,
            "integrity": integrity,
            **dual_quality,
        }
        _validate_release_schema(
            snapshot_payload, "release_snapshot.v1.schema.json"
        )
        _validate_release_schema(quality_payload, "project_release.v2.schema.json")
        staged[docx] = _stage_release_file(docx, docx_bytes)
        staged[quality] = _stage_release_file(quality, _json_bytes(quality_payload))
        staged[release_snapshot] = _stage_release_file(
            release_snapshot, _json_bytes(snapshot_payload)
        )
        _commit_release_files(staged)
        staged = {}
        result = {
            "status": release_level,
            "release_status": release_level,
            "release_level": release_level,
            "placeholder_count": figure_validation["placeholder_count"],
            "pending_placeholder_count": figure_validation["pending_placeholder_count"],
            "manuscript_sha256": manuscript_sha256,
            "release_markdown_sha256": release_markdown_sha256,
            "chemical_paper_binding_digest": chemical_paper["binding_digest"],
            "chemical_paper_safe_summary": chemical_paper_safe,
            "chemical_paper_dependency_can_release": chemical_paper[
                "dependency_currentness"
            ]["can_release"],
            "docx_sha256": docx_sha256,
            "workflow_digest": workflow_digest,
            "snapshot": snapshot,
            "docx": docx,
            "release_snapshot": release_snapshot,
            "quality_report": quality,
        }
        result.update(dual_quality)
        return result
    finally:
        if temporary_docx is not None:
            temporary_docx.unlink(missing_ok=True)
        for path in staged.values():
            path.unlink(missing_ok=True)


def build_project_release(
    project: Path,
    python_executable: Path = Path(sys.executable),
    *,
    release_level: str | None = None,
) -> dict[str, Any]:
    """Snapshot and export one validated authoritative manuscript without editing it."""
    with PROJECT_RELEASE_LOCK:
        project_path = Path(project)
        workflow = workflow_state(project_path)
        if workflow["route"] == NEW_ROUTE:
            return _new_route_release(
                project_path,
                workflow,
                release_level=release_level or "SELF_REVIEWED_DRAFT",
                python_executable=python_executable,
            )
        if release_level not in {None, "SELF_REVIEWED_DRAFT"}:
            raise ProjectReleaseError(
                "RELEASE_LEVEL_INVALID", "legacy projects do not support expert release levels"
            )
        return _build_project_release_unlocked(project_path, python_executable)


def new_route_release_docx_is_current(docx_path: Path) -> bool:
    """Revalidate every authoritative binding before serving a new-route DOCX."""
    candidate = Path(docx_path)
    expected_levels = {
        "self_reviewed_draft.docx": "SELF_REVIEWED_DRAFT",
        "expert_reviewed_release.docx": "EXPERT_REVIEWED_RELEASE",
    }
    release_level = expected_levels.get(candidate.name)
    if release_level is None or candidate.parent.name != "05_release":
        return False
    project = candidate.parent.parent
    markdown_name = candidate.with_suffix(".md").name
    relatives = (
        Path("04_manuscript/manuscript.md"),
        Path("04_manuscript/manuscript_lineage.v2.json"),
        Path("03_figures/source_figure_registry.json"),
        Path("03_figures/synthesis_figure_placeholders.json"),
        Path(f"05_release/{markdown_name}"),
        Path(f"05_release/{candidate.name}"),
        Path("05_release/release_snapshot.json"),
        Path("05_release/quality_report.json"),
    )
    try:
        project = project.resolve(strict=True)
        validate_project_path_components(project, relatives)
        source = validate_project_file_path(
            project, relatives[0], "MANUSCRIPT_INVALID"
        )
        lineage_path = validate_project_file_path(
            project, relatives[1], "MANUSCRIPT_LINEAGE_INVALID"
        )
        released_markdown = validate_project_file_path(
            project, relatives[4], "RELEASE_SNAPSHOT_INVALID"
        )
        released_docx = validate_project_file_path(
            project, relatives[5], "DOCX_ZIP_INVALID"
        )
        release_snapshot_path = validate_project_file_path(
            project, relatives[6], "RELEASE_SNAPSHOT_INVALID"
        )
        quality_path = validate_project_file_path(
            project, relatives[7], "QUALITY_REPORT_INVALID"
        )
        if released_docx.resolve(strict=True) != candidate.resolve(strict=True):
            return False
        snapshot = _read_json(release_snapshot_path, "RELEASE_SNAPSHOT_INVALID")
        quality = _read_json(quality_path, "QUALITY_REPORT_INVALID")
        lineage = _read_json(lineage_path, "MANUSCRIPT_LINEAGE_INVALID")
        if not all(isinstance(value, dict) for value in (snapshot, quality, lineage)):
            return False
        _validate_release_schema(snapshot, "release_snapshot.v1.schema.json")
        _validate_release_schema(quality, "project_release.v2.schema.json")
        expected_markdown_relative = f"05_release/{markdown_name}"
        expected_docx_relative = f"05_release/{candidate.name}"
        if (
            snapshot.get("schema_version") != "release-snapshot.v1"
            or snapshot.get("route") not in {NEW_ROUTE, HONEST_PROGRESSIVE_ROUTE}
            or snapshot.get("release_level") != release_level
            or snapshot.get("status") != release_level
            or snapshot.get("markdown_path") != expected_markdown_relative
            or snapshot.get("docx_path") != expected_docx_relative
            or quality.get("schema_version") != "project-release.v2"
            or quality.get("release_level") != release_level
            or quality.get("status") != release_level
        ):
            return False
        manuscript_bytes = source.read_bytes()
        manuscript_sha256 = _sha256_bytes(manuscript_bytes)
        lineage_digest = lineage.get("lineage_digest")
        docx_sha256 = _sha256_bytes(released_docx.read_bytes())
        workflow = workflow_state(project)
        workflow_digest = workflow.get("workflow_digest")
        authoritative = manuscript_state(project)
        if (
            workflow.get("route") != NEW_ROUTE
            or workflow.get("internal_draft_export_ready") is not True
            or not isinstance(workflow_digest, str)
            or snapshot.get("workflow_digest") != workflow_digest
            or snapshot.get("lineage_digest") != lineage_digest
            or snapshot.get("manuscript_sha256") != manuscript_sha256
            or snapshot.get("docx_sha256") != docx_sha256
            or quality.get("workflow_digest") != workflow_digest
            or quality.get("lineage_digest") != lineage_digest
            or quality.get("manuscript_sha256") != manuscript_sha256
            or quality.get("docx_sha256") != docx_sha256
            or not isinstance(authoritative, dict)
            or authoritative.get("workflow_can_continue") is not True
            or authoritative.get("manuscript_sha256") != manuscript_sha256
            or authoritative.get("lineage_digest") != lineage_digest
        ):
            return False
        if snapshot.get("route") == HONEST_PROGRESSIVE_ROUTE:
            try:
                snapshot_honest = honest_progressive_summary_from_projection(
                    snapshot, project_scope=True
                )
                quality_honest = honest_progressive_summary_from_projection(
                    quality, project_scope=True
                )
            except PaperEvidenceError:
                return False
            if snapshot_honest is None or snapshot_honest != quality_honest:
                return False
        markdown = manuscript_bytes.decode("utf-8")
        chemical_paper = _chemical_paper_release_state(project, lineage)
        if (
            release_level == "EXPERT_REVIEWED_RELEASE"
            and not chemical_paper["dependency_currentness"]["can_release"]
        ):
            return False
        release_markdown = release_markdown_with_chemical_limitations(
            markdown, chemical_paper
        )
        release_markdown_bytes = release_markdown.encode("utf-8")
        release_markdown_sha256 = _sha256_bytes(release_markdown_bytes)
        chemical_paper_safe = safe_chemical_paper_projection(chemical_paper)
        if (
            released_markdown.read_bytes() != release_markdown_bytes
            or snapshot.get("release_markdown_sha256") != release_markdown_sha256
            or quality.get("release_markdown_sha256") != release_markdown_sha256
            or snapshot.get("chemical_paper_binding_digest")
            != chemical_paper["binding_digest"]
            or quality.get("chemical_paper_binding_digest")
            != chemical_paper["binding_digest"]
            or snapshot.get("chemical_paper_safe_summary") != chemical_paper_safe
            or quality.get("chemical_paper_safe_summary") != chemical_paper_safe
            or snapshot.get("chemical_paper_dependency_can_release")
            is not chemical_paper["dependency_currentness"]["can_release"]
            or quality.get("chemical_paper_dependency_can_release")
            is not chemical_paper["dependency_currentness"]["can_release"]
        ):
            return False
        dual_source_root = project / "01_evidence/dual_source"
        has_dual_lineage = isinstance(lineage.get("dual_parse_bindings"), list)
        if dual_source_root.exists() or has_dual_lineage:
            if (
                dual_source_root.is_symlink()
                or not dual_source_root.is_dir()
                or not has_dual_lineage
            ):
                return False
            dual_state = dual_parse_release_state(project)
            if (
                dual_state.get("internal_release_ready") is not True
                or dual_state.get("hard_fails") != []
                or quality.get("dual_parse_status") != "current"
                or quality.get("dual_parse_binding_digest")
                != canonical_digest(lineage["dual_parse_bindings"])
                or quality.get("reaction_data_status")
                != dual_state.get("reaction_data_status")
                or quality.get("reaction_count") != dual_state.get("reaction_count")
                or quality.get("credits_status")
                != "NOT_APPLICABLE_BY_CURRENT_SCOPE"
            ):
                return False
        figure_validation = _new_route_figure_state(
            project,
            markdown,
            release_level=release_level,
            lineage_digest=lineage_digest,
        )
        if quality.get("figure_validation") != figure_validation:
            return False
        legacy_docx = project / "05_final_audit/final_draft.docx"
        integrity = validate_docx_integrity(
            released_docx,
            markdown=release_markdown,
            expected_media_sha256=figure_validation["expected_media_sha256"],
            required_attributions=figure_validation["required_attributions"],
            workflow_digest=workflow_digest,
            snapshot_workflow_digest=snapshot["workflow_digest"],
            expected_project_id=project.name,
            expected_release_level=release_level,
            legacy_docx=(
                legacy_docx
                if legacy_docx.is_file() and not legacy_docx.is_symlink()
                else None
            ),
        )
        if snapshot.get("integrity") != integrity or quality.get("integrity") != integrity:
            return False
        if release_level == "EXPERT_REVIEWED_RELEASE":
            from review_writer.evaluation.review_benchmark import (
                verified_synthesis_figures_current,
            )

            if not verified_synthesis_figures_current(
                project,
                lineage_digest=lineage_digest,
                manuscript_text=markdown,
                docx_path=released_docx,
            ):
                return False
        return True
    except (
        DocxIntegrityError,
        FigurePolicyError,
        OSError,
        ProjectReleaseError,
        UnicodeError,
        ValueError,
    ):
        return False


def _build_project_release_unlocked(
    project: Path,
    python_executable: Path,
) -> dict[str, Any]:
    project_path = Path(project)
    workflow = workflow_state(project_path)
    if workflow["route"] == NEW_ROUTE:
        if not workflow["parse_ready"]:
            raise ProjectReleaseError(
                "PARSE_QUALITY_NOT_READY",
                "source-truth parse review must close before release",
            )
        if not workflow["internal_draft_export_ready"]:
            raise ProjectReleaseError(
                "REVIEW_WORKFLOW_NOT_READY",
                "evidence-to-release review must close before release",
            )
    source = validate_project_file_path(
        project_path, Path("04_first_draft/first_draft.md"), "MANUSCRIPT_INVALID"
    )
    try:
        manuscript_bytes = source.read_bytes()
        markdown = manuscript_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProjectReleaseError("MANUSCRIPT_INVALID", "authoritative manuscript must be readable UTF-8") from exc

    validation = validate_manuscript_lineage(project_path, markdown)
    figure_validation = _release_figure_validation(
        project_path,
        approved_claim_ids=validation["approved_claim_ids"],
        manuscript_sha256=validation["manuscript_sha256"],
        manuscript_image_paths=validation["image_paths"],
        manuscript_markdown=markdown,
    )
    stage = project_path / "05_final_audit"
    snapshot = stage / "final_draft.md"
    docx = stage / "final_draft.docx"
    quality = stage / "quality_report.json"
    release_paths = (snapshot, docx, quality)
    _reject_reparse_components(
        project_path,
        tuple(path.relative_to(project_path) for path in release_paths),
    )
    previous = {path: path.read_bytes() if path.is_file() else None for path in release_paths}
    converter = Path(__file__).resolve().parents[2] / "skills" / "review-export-docx" / "scripts" / "md2docx.py"
    temporary_docx: Path | None = None

    try:
        _atomic_write(snapshot, manuscript_bytes)
        if not converter.is_file():
            raise ProjectReleaseError("DOCX_CONVERTER_MISSING", "repository DOCX converter is unavailable")
        with tempfile.NamedTemporaryFile(
            dir=stage,
            prefix=".final_draft.",
            suffix=".docx.tmp",
            delete=False,
        ) as handle:
            temporary_docx = Path(handle.name)
        temporary_docx.unlink()
        try:
            completed = subprocess.run(
                [
                    str(Path(python_executable)),
                    str(converter),
                    "--input",
                    str(snapshot),
                    "--output",
                    str(temporary_docx),
                    "--project-id",
                    project_path.name,
                    "--release-level",
                    "SELF_REVIEWED_DRAFT",
                ],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProjectReleaseError("DOCX_EXPORT_FAILED", "DOCX converter could not complete") from exc
        if completed.returncode != 0 or not temporary_docx.is_file() or temporary_docx.stat().st_size <= 0:
            raise ProjectReleaseError("DOCX_EXPORT_FAILED", "DOCX converter did not produce a document")
        if not zipfile.is_zipfile(temporary_docx):
            raise ProjectReleaseError("DOCX_EXPORT_FAILED", "DOCX converter output is invalid")
        _validate_docx_attributions(temporary_docx, figure_validation)

        docx_bytes = temporary_docx.read_bytes()
        docx_sha256 = _sha256_bytes(docx_bytes)
        status = _release_status(project_path)
        existing_report = _read_json(quality, "QUALITY_REPORT_INVALID") if quality.is_file() else {}
        if not isinstance(existing_report, dict):
            raise ProjectReleaseError("QUALITY_REPORT_INVALID", "quality report must be an object")
        report = {
            **existing_report,
            "schema_version": "project-release.v1",
            "status": status,
            "release_status": status,
            "manuscript_sha256": validation["manuscript_sha256"],
            "docx_sha256": docx_sha256,
            "figure_validation": figure_validation,
            "release": {
                "status": status,
                "manuscript_sha256": validation["manuscript_sha256"],
                "docx_sha256": docx_sha256,
            },
        }
        _atomic_write(docx, docx_bytes)
        _atomic_write(quality, _json_bytes(report))
        return {
            "status": status,
            "release_status": status,
            "manuscript_sha256": validation["manuscript_sha256"],
            "docx_sha256": docx_sha256,
            "snapshot": snapshot,
            "docx": docx,
            "quality_report": quality,
        }
    except Exception:
        _restore_release(release_paths, previous)
        raise
    finally:
        if temporary_docx is not None:
            temporary_docx.unlink(missing_ok=True)
