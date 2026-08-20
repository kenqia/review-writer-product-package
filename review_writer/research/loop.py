"""Topic-neutral research-loop contracts for corpus and evidence review.

The module is deliberately in-memory. It validates researcher-facing metadata,
keeps duplicate and divergent lineages visible, and never promotes a matrix
row into scientific or human acceptance.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, NoReturn

from review_writer.acquisition.manifest_identity import normalize_doi


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
DOCUMENT_ROLES = frozenset({"MAIN", "SI"})
EVIDENCE_STATUSES = frozenset(
    {"AI_PROVISIONAL", "CONFIRMED", "GAP", "NON_COMPARABLE", "BLOCKED"}
)
_COVERED_STATUSES = frozenset({"AI_PROVISIONAL", "CONFIRMED", "NON_COMPARABLE"})


class ResearchValidationError(ValueError):
    """A research-loop input cannot be represented without guessing."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


def _fail(code: str, message: str) -> NoReturn:
    raise ResearchValidationError(code, message)


def _json_copy(value: Any, code: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ResearchValidationError(code, "value must be finite JSON data") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _text(value: Any, code: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(code, f"{field_name} must be a nonempty string")
    return value.strip()


def _identifier(value: Any, code: str, field_name: str) -> str:
    normalized = _text(value, code, field_name)
    if not IDENTIFIER_RE.fullmatch(normalized):
        _fail(code, f"{field_name} must be a portable identifier")
    return normalized


def _optional_text(value: Any, code: str, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, code, field_name)


def _unique_texts(value: Any, code: str, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        _fail(code, f"{field_name} must be a list of strings")
    result: list[str] = []
    for item in value:
        normalized = _text(item, code, field_name)
        if normalized in result:
            _fail(code, f"{field_name} must not contain duplicates")
        result.append(normalized)
    return tuple(result)


def _question(value: Any, index: int) -> ResearchQuestion:
    if isinstance(value, ResearchQuestion):
        return value
    if isinstance(value, Mapping):
        question_id = value.get("question_id", value.get("id"))
        text = value.get("text", value.get("question", value.get("prompt")))
        return ResearchQuestion(question_id=question_id, text=text)
    if isinstance(value, str):
        return ResearchQuestion(question_id=f"RQ{index}", text=value)
    _fail("REVIEW_QUESTION_INVALID", "each review question must be an object or string")


@dataclass(frozen=True, slots=True)
class ResearchQuestion:
    """One stable, topic-independent review question."""

    question_id: str
    text: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "question_id",
            _identifier(self.question_id, "REVIEW_QUESTION_INVALID", "question_id"),
        )
        object.__setattr__(
            self,
            "text",
            _text(self.text, "REVIEW_QUESTION_INVALID", "text"),
        )

    @property
    def id(self) -> str:
        return self.question_id

    @property
    def prompt(self) -> str:
        return self.text

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ResearchQuestion:
        return _question(value, 1)

    def to_dict(self) -> dict[str, str]:
        return {"question_id": self.question_id, "text": self.text}


ReviewQuestion = ResearchQuestion


@dataclass(frozen=True, slots=True)
class ReviewScope:
    """The bounded topic, questions, and inclusion/exclusion contract."""

    topic: str
    review_questions: tuple[ResearchQuestion, ...]
    inclusion_criteria: tuple[str, ...] = ()
    exclusion_criteria: tuple[str, ...] = ()
    from_year: int | None = None
    to_year: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "topic",
            _text(self.topic, "SCOPE_INVALID", "topic"),
        )
        if isinstance(self.review_questions, (str, bytes)):
            _fail("REVIEW_QUESTION_INVALID", "review_questions must be a nonempty sequence")
        try:
            questions = tuple(
                _question(question, index)
                for index, question in enumerate(self.review_questions, start=1)
            )
        except TypeError as exc:
            raise ResearchValidationError(
                "REVIEW_QUESTION_INVALID", "review_questions must be a sequence"
            ) from exc
        if not questions:
            _fail("REVIEW_QUESTION_REQUIRED", "at least one review question is required")
        question_ids = [question.question_id for question in questions]
        if len(question_ids) != len(set(question_ids)):
            _fail("DUPLICATE_QUESTION_ID", "review question IDs must be unique")
        object.__setattr__(self, "review_questions", questions)
        object.__setattr__(
            self,
            "inclusion_criteria",
            _unique_texts(self.inclusion_criteria, "SCOPE_INVALID", "inclusion_criteria"),
        )
        object.__setattr__(
            self,
            "exclusion_criteria",
            _unique_texts(self.exclusion_criteria, "SCOPE_INVALID", "exclusion_criteria"),
        )
        for field_name, value in (("from_year", self.from_year), ("to_year", self.to_year)):
            if value is not None and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 1 <= value <= 9999
            ):
                _fail("SCOPE_INVALID", f"{field_name} must be a year from 1 to 9999")
        if self.from_year is not None and self.to_year is not None and self.from_year > self.to_year:
            _fail("SCOPE_INVALID", "from_year must not exceed to_year")

    @property
    def questions(self) -> tuple[ResearchQuestion, ...]:
        return self.review_questions

    @property
    def question_ids(self) -> tuple[str, ...]:
        return tuple(question.question_id for question in self.review_questions)

    @property
    def scope_digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "topic": self.topic,
            "review_questions": [question.to_dict() for question in self.review_questions],
            "inclusion_criteria": list(self.inclusion_criteria),
            "exclusion_criteria": list(self.exclusion_criteria),
        }
        if self.from_year is not None:
            result["from_year"] = self.from_year
        if self.to_year is not None:
            result["to_year"] = self.to_year
        return result


Scope = ReviewScope


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    """A MAIN or SI metadata record; no file is opened by this package."""

    source_id: str
    document_role: str
    sha256: str | None = None
    locator: Any | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_id",
            _identifier(self.source_id, "SOURCE_INVALID", "source_id"),
        )
        role = _text(self.document_role, "SOURCE_INVALID", "document_role").upper()
        if role not in DOCUMENT_ROLES:
            _fail("SOURCE_INVALID", "document_role must be MAIN or SI")
        object.__setattr__(self, "document_role", role)
        if self.sha256 is not None:
            digest = _text(self.sha256, "SOURCE_INVALID", "sha256").lower()
            if not SHA256_RE.fullmatch(digest):
                _fail("SOURCE_INVALID", "sha256 must contain exactly 64 hexadecimal characters")
            object.__setattr__(self, "sha256", digest)
        object.__setattr__(
            self,
            "locator",
            _json_copy(self.locator, "PROVENANCE_INVALID") if self.locator is not None else None,
        )
        if self.provenance is None:
            provenance: dict[str, Any] = {}
        elif isinstance(self.provenance, Mapping):
            provenance = _json_copy(dict(self.provenance), "PROVENANCE_INVALID")
        else:
            _fail("PROVENANCE_INVALID", "provenance must be a JSON object")
        object.__setattr__(self, "provenance", provenance)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CorpusDocument:
        if not isinstance(value, Mapping):
            _fail("SOURCE_INVALID", "document must be an object")
        source_id = value.get("source_id", value.get("document_id", value.get("id")))
        role = value.get("document_role", value.get("role"))
        return cls(
            source_id=source_id,
            document_role=role,
            sha256=value.get("sha256"),
            locator=value.get("locator"),
            provenance=value.get("provenance", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source_id": self.source_id,
            "document_role": self.document_role,
        }
        if self.sha256 is not None:
            result["sha256"] = self.sha256
        if self.locator is not None:
            result["locator"] = copy.deepcopy(self.locator)
        if self.provenance:
            result["provenance"] = copy.deepcopy(self.provenance)
        return result


SourceDocument = CorpusDocument


def _coverage_ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        value = [key for key, state in value.items() if state not in (False, None, "GAP")]
    return _unique_texts(value, "COVERAGE_INVALID", "covered_question_ids")


@dataclass(frozen=True, slots=True)
class CorpusStudy:
    """One source lineage kept intact even when it duplicates another study."""

    study_id: str
    title: str = ""
    doi: str | None = None
    source_role: str = "PRIMARY"
    lineage_id: str | None = None
    documents: tuple[CorpusDocument, ...] = ()
    covered_question_ids: tuple[str, ...] = ()
    duplicate_of: str | None = None

    def __post_init__(self) -> None:
        study_id = _identifier(self.study_id, "STUDY_INVALID", "study_id")
        object.__setattr__(self, "study_id", study_id)
        title = "" if self.title is None else _text(self.title, "STUDY_INVALID", "title")
        object.__setattr__(self, "title", title)
        source_role = _text(self.source_role, "SOURCE_ROLE_INVALID", "source_role")
        object.__setattr__(self, "source_role", source_role)
        lineage_id = self.lineage_id or study_id
        object.__setattr__(
            self,
            "lineage_id",
            _identifier(lineage_id, "LINEAGE_INVALID", "lineage_id"),
        )
        if self.doi is not None:
            normalized_doi = normalize_doi(self.doi)
            if normalized_doi is None:
                _fail("DOI_INVALID", "doi must be a valid DOI when provided")
            object.__setattr__(self, "doi", normalized_doi)
        documents: list[CorpusDocument] = []
        for raw_document in self.documents:
            document = (
                raw_document
                if isinstance(raw_document, CorpusDocument)
                else CorpusDocument.from_mapping(raw_document)
            )
            if any(existing.source_id == document.source_id for existing in documents):
                _fail("DUPLICATE_SOURCE_ID", "document source IDs must be unique within a study")
            documents.append(document)
        object.__setattr__(self, "documents", tuple(documents))
        object.__setattr__(
            self,
            "covered_question_ids",
            _coverage_ids(self.covered_question_ids),
        )
        if self.duplicate_of is not None:
            object.__setattr__(
                self,
                "duplicate_of",
                _identifier(self.duplicate_of, "DUPLICATE_INVALID", "duplicate_of"),
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CorpusStudy:
        if not isinstance(value, Mapping):
            _fail("STUDY_INVALID", "corpus records must be objects")
        raw_documents = value.get("documents")
        if raw_documents is None and value.get("document_role") is not None:
            raw_documents = [value]
        elif isinstance(raw_documents, Mapping):
            raw_documents = [raw_documents]
        elif raw_documents is None:
            raw_documents = []
        return cls(
            study_id=value.get("study_id"),
            title=value.get("title", value.get("name", "")),
            doi=value.get("doi"),
            source_role=value.get("source_role", value.get("role", value.get("tier", "PRIMARY"))),
            lineage_id=value.get("lineage_id", value.get("lineage")),
            documents=tuple(raw_documents),
            covered_question_ids=value.get(
                "covered_question_ids",
                value.get("coverage"),
            ),
            duplicate_of=value.get("duplicate_of"),
        )

    @property
    def main_documents(self) -> tuple[CorpusDocument, ...]:
        return tuple(document for document in self.documents if document.document_role == "MAIN")

    @property
    def si_documents(self) -> tuple[CorpusDocument, ...]:
        return tuple(document for document in self.documents if document.document_role == "SI")

    @property
    def si_state(self) -> str:
        count = len(self.si_documents)
        if count == 0:
            return "MISSING"
        if count == 1:
            return "PRESENT"
        return "DUPLICATE"

    @property
    def has_si(self) -> bool:
        return bool(self.si_documents)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "study_id": self.study_id,
            "title": self.title,
            "source_role": self.source_role,
            "lineage_id": self.lineage_id,
            "documents": [document.to_dict() for document in self.documents],
            "covered_question_ids": list(self.covered_question_ids),
            "si_state": self.si_state,
        }
        if self.doi is not None:
            result["doi"] = self.doi
        if self.duplicate_of is not None:
            result["duplicate_of"] = self.duplicate_of
        return result


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    """A visible duplicate relation; members are never silently merged."""

    key: str
    reason: str
    study_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "reason": self.reason, "study_ids": list(self.study_ids)}


def _title_key(title: str) -> str | None:
    if not title:
        return None
    normalized = re.sub(r"[^\w]+", " ", title.casefold(), flags=re.UNICODE).strip()
    return " ".join(normalized.split()) or None


@dataclass(frozen=True, slots=True)
class Corpus:
    """An immutable, metadata-only corpus with explicit duplicate and SI state."""

    records: tuple[CorpusStudy, ...]

    def __post_init__(self) -> None:
        normalized: list[CorpusStudy] = []
        for raw_record in self.records:
            record = (
                raw_record
                if isinstance(raw_record, CorpusStudy)
                else CorpusStudy.from_mapping(raw_record)
            )
            if any(existing.study_id == record.study_id for existing in normalized):
                _fail("DUPLICATE_STUDY_ID", "study IDs must be unique in one corpus")
            normalized.append(record)
        if not normalized:
            _fail("CORPUS_EMPTY", "corpus must contain at least one study")
        object.__setattr__(self, "records", tuple(normalized))

    @classmethod
    def from_records(cls, records: Corpus | Iterable[CorpusStudy | Mapping[str, Any]]) -> Corpus:
        if isinstance(records, cls):
            return records
        if isinstance(records, Mapping):
            records = records.get("studies", records.get("records"))
        if records is None or isinstance(records, (str, bytes)):
            _fail("CORPUS_INVALID", "corpus records must be an iterable")
        try:
            return cls(tuple(records))
        except TypeError as exc:
            raise ResearchValidationError("CORPUS_INVALID", "corpus records must be an iterable") from exc

    @property
    def studies(self) -> tuple[CorpusStudy, ...]:
        return self.records

    def get(self, study_id: str) -> CorpusStudy:
        for record in self.records:
            if record.study_id == study_id:
                return record
        _fail("STUDY_UNKNOWN", f"unknown study_id: {study_id}")

    def duplicate_groups(self) -> tuple[DuplicateGroup, ...]:
        groups: list[DuplicateGroup] = []
        seen: set[frozenset[str]] = set()

        def append_group(key: str, reason: str, members: Iterable[CorpusStudy]) -> None:
            member_ids = tuple(record.study_id for record in self.records if record in tuple(members))
            if len(member_ids) < 2:
                return
            identity = frozenset(member_ids)
            if identity in seen:
                return
            seen.add(identity)
            groups.append(DuplicateGroup(key=key, reason=reason, study_ids=member_ids))

        by_doi: dict[str, list[CorpusStudy]] = {}
        by_title: dict[str, list[CorpusStudy]] = {}
        for record in self.records:
            if record.doi is not None:
                by_doi.setdefault(record.doi, []).append(record)
            elif (title_key := _title_key(record.title)) is not None:
                by_title.setdefault(title_key, []).append(record)
        for key, members in by_doi.items():
            append_group(key, "doi", members)
        for key, members in by_title.items():
            append_group(key, "title", members)
        for record in self.records:
            if record.duplicate_of is None:
                continue
            try:
                target = self.get(record.duplicate_of)
            except ResearchValidationError:
                _fail("DUPLICATE_TARGET_UNKNOWN", f"duplicate_of target is not in the corpus: {record.duplicate_of}")
            append_group(f"{target.study_id}:{record.study_id}", "explicit", (target, record))
        return tuple(groups)

    def is_duplicate(self, study_id: str) -> bool:
        return any(study_id in group.study_ids for group in self.duplicate_groups())

    def view(
        self,
        *,
        source_role: str | None = None,
        has_si: bool | None = None,
        si_state: str | None = None,
        duplicate: bool | None = None,
        coverage_question_id: str | None = None,
    ) -> tuple[CorpusStudy, ...]:
        role_key = source_role.casefold() if source_role is not None else None
        state_key = si_state.upper() if si_state is not None else None
        if state_key is not None and state_key not in {"MISSING", "PRESENT", "DUPLICATE"}:
            _fail("SI_STATE_INVALID", "si_state must be MISSING, PRESENT, or DUPLICATE")
        return tuple(
            record
            for record in self.records
            if (role_key is None or record.source_role.casefold() == role_key)
            and (has_si is None or record.has_si is has_si)
            and (state_key is None or record.si_state == state_key)
            and (duplicate is None or self.is_duplicate(record.study_id) is duplicate)
            and (
                coverage_question_id is None
                or coverage_question_id in record.covered_question_ids
            )
        )

    def coverage(self, scope: ReviewScope) -> dict[str, dict[str, Any]]:
        if not isinstance(scope, ReviewScope):
            _fail("SCOPE_INVALID", "coverage requires a ReviewScope")
        result: dict[str, dict[str, Any]] = {}
        for question_id in scope.question_ids:
            declared = tuple(
                record.study_id
                for record in self.records
                if question_id in record.covered_question_ids
            )
            result[question_id] = {
                "question_id": question_id,
                "study_count": len(self.records),
                "declared_study_ids": declared,
                "missing_study_ids": tuple(
                    record.study_id
                    for record in self.records
                    if record.study_id not in declared
                ),
                "coverage_ratio": len(declared) / len(self.records),
            }
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "studies": [record.to_dict() for record in self.records],
            "duplicate_groups": [group.to_dict() for group in self.duplicate_groups()],
        }


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One study/question cell, retaining its status and provenance."""

    study_id: str
    question_id: str
    status: str
    source_role: str | None = None
    lineage_id: str | None = None
    statement: str | None = None
    gap_reason: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "study_id",
            _identifier(self.study_id, "EVIDENCE_INVALID", "study_id"),
        )
        object.__setattr__(
            self,
            "question_id",
            _identifier(self.question_id, "EVIDENCE_INVALID", "question_id"),
        )
        status = _text(self.status, "EVIDENCE_STATUS_INVALID", "status").upper()
        if status not in EVIDENCE_STATUSES:
            _fail("EVIDENCE_STATUS_INVALID", f"unsupported evidence status: {status}")
        object.__setattr__(self, "status", status)
        if self.source_role is not None:
            object.__setattr__(
                self,
                "source_role",
                _text(self.source_role, "SOURCE_ROLE_INVALID", "source_role"),
            )
        if self.lineage_id is not None:
            object.__setattr__(
                self,
                "lineage_id",
                _identifier(self.lineage_id, "LINEAGE_INVALID", "lineage_id"),
            )
        if self.statement is not None:
            object.__setattr__(
                self,
                "statement",
                _text(self.statement, "EVIDENCE_INVALID", "statement"),
            )
        if self.gap_reason is not None:
            object.__setattr__(
                self,
                "gap_reason",
                _text(self.gap_reason, "EVIDENCE_INVALID", "gap_reason"),
            )
        if self.provenance is None:
            provenance: dict[str, Any] = {}
        elif isinstance(self.provenance, Mapping):
            provenance = _json_copy(dict(self.provenance), "PROVENANCE_INVALID")
        else:
            _fail("PROVENANCE_INVALID", "provenance must be a JSON object")
        if self.status != "GAP" and not provenance:
            _fail("PROVENANCE_REQUIRED", "non-GAP evidence requires provenance")
        object.__setattr__(self, "provenance", provenance)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EvidenceRecord:
        if not isinstance(value, Mapping):
            _fail("EVIDENCE_INVALID", "evidence rows must be objects")
        return cls(
            study_id=value.get("study_id"),
            question_id=value.get("question_id", value.get("rq_id")),
            status=value.get("status"),
            source_role=value.get("source_role"),
            lineage_id=value.get("lineage_id", value.get("lineage")),
            statement=value.get("statement", value.get("claim")),
            gap_reason=value.get("gap_reason", value.get("reason")),
            provenance=value.get("provenance", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "study_id": self.study_id,
            "question_id": self.question_id,
            "status": self.status,
            "provenance": copy.deepcopy(self.provenance),
        }
        for name in ("source_role", "lineage_id", "statement", "gap_reason"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result


EvidenceRow = EvidenceRecord


@dataclass(frozen=True, slots=True)
class EvidenceMatrix:
    """A filterable evidence view with explicit GAP rows for missing cells."""

    scope: ReviewScope
    corpus: Corpus
    rows: tuple[EvidenceRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ReviewScope):
            _fail("SCOPE_INVALID", "matrix requires a ReviewScope")
        if not isinstance(self.corpus, Corpus):
            _fail("CORPUS_INVALID", "matrix requires a Corpus")
        normalized = tuple(
            row if isinstance(row, EvidenceRecord) else EvidenceRecord.from_mapping(row)
            for row in self.rows
        )
        seen: set[tuple[str, str, str]] = set()
        for row in normalized:
            key = (row.study_id, row.question_id, row.lineage_id or "")
            if key in seen:
                _fail("DUPLICATE_EVIDENCE_ROW", "one lineage may have only one evidence row per question")
            seen.add(key)
        object.__setattr__(self, "rows", normalized)

    @classmethod
    def from_records(
        cls,
        *,
        scope: ReviewScope,
        corpus: Corpus | Iterable[CorpusStudy | Mapping[str, Any]],
        evidence: Iterable[EvidenceRecord | Mapping[str, Any]] | None = None,
    ) -> EvidenceMatrix:
        if not isinstance(scope, ReviewScope):
            _fail("SCOPE_INVALID", "matrix requires a ReviewScope")
        normalized_corpus = Corpus.from_records(corpus)
        raw_evidence = () if evidence is None else evidence
        if isinstance(raw_evidence, (str, bytes)):
            _fail("EVIDENCE_INVALID", "evidence must be an iterable")
        try:
            candidates = tuple(raw_evidence)
        except TypeError as exc:
            raise ResearchValidationError("EVIDENCE_INVALID", "evidence must be an iterable") from exc

        rows: list[EvidenceRecord] = []
        seen_pairs: set[tuple[str, str]] = set()
        seen_lineages: set[tuple[str, str, str]] = set()
        for raw_row in candidates:
            row = raw_row if isinstance(raw_row, EvidenceRecord) else EvidenceRecord.from_mapping(raw_row)
            study = normalized_corpus.get(row.study_id)
            if row.question_id not in scope.question_ids:
                _fail("UNKNOWN_REVIEW_QUESTION", f"unknown question_id: {row.question_id}")
            row = replace(
                row,
                source_role=row.source_role or study.source_role,
                lineage_id=row.lineage_id or study.lineage_id,
            )
            pair = (row.study_id, row.question_id)
            lineage_key = (row.study_id, row.question_id, row.lineage_id or "")
            if lineage_key in seen_lineages:
                _fail("DUPLICATE_EVIDENCE_ROW", "one lineage may have only one evidence row per question")
            rows.append(row)
            seen_pairs.add(pair)
            seen_lineages.add(lineage_key)

        for study in normalized_corpus.records:
            for question_id in scope.question_ids:
                if (study.study_id, question_id) not in seen_pairs:
                    rows.append(
                        EvidenceRecord(
                            study_id=study.study_id,
                            question_id=question_id,
                            status="GAP",
                            source_role=study.source_role,
                            lineage_id=study.lineage_id,
                            gap_reason="NO_EVIDENCE_ROW",
                        )
                    )
        return cls(scope=scope, corpus=normalized_corpus, rows=tuple(rows))

    def view(
        self,
        *,
        question_id: str | None = None,
        study_id: str | None = None,
        status: str | None = None,
        source_role: str | None = None,
        lineage_id: str | None = None,
        include_gaps: bool = True,
    ) -> tuple[EvidenceRecord, ...]:
        normalized_status = status.upper() if status is not None else None
        if normalized_status is not None and normalized_status not in EVIDENCE_STATUSES:
            _fail("EVIDENCE_STATUS_INVALID", f"unsupported evidence status: {normalized_status}")
        role_key = source_role.casefold() if source_role is not None else None
        return tuple(
            row
            for row in self.rows
            if (question_id is None or row.question_id == question_id)
            and (study_id is None or row.study_id == study_id)
            and (normalized_status is None or row.status == normalized_status)
            and (role_key is None or (row.source_role or "").casefold() == role_key)
            and (lineage_id is None or row.lineage_id == lineage_id)
            and (include_gaps or row.status != "GAP")
        )

    def filter_rows(self, **filters: Any) -> tuple[EvidenceRecord, ...]:
        return self.view(**filters)

    def gaps(
        self,
        *,
        question_id: str | None = None,
        study_id: str | None = None,
        source_role: str | None = None,
    ) -> tuple[EvidenceRecord, ...]:
        return self.view(
            question_id=question_id,
            study_id=study_id,
            source_role=source_role,
            status="GAP",
        )

    def provenance_for(self, study_id: str, question_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(
            copy.deepcopy(row.provenance)
            for row in self.rows
            if row.study_id == study_id and row.question_id == question_id and row.provenance
        )

    def coverage(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for question_id in self.scope.question_ids:
            covered: list[str] = []
            gaps: list[str] = []
            blocked: list[str] = []
            non_comparable: list[str] = []
            row_count = 0
            for study in self.corpus.records:
                matches = [
                    row
                    for row in self.rows
                    if row.study_id == study.study_id and row.question_id == question_id
                ]
                row_count += len(matches)
                statuses = {row.status for row in matches}
                if statuses & _COVERED_STATUSES:
                    covered.append(study.study_id)
                elif statuses == {"BLOCKED"}:
                    blocked.append(study.study_id)
                else:
                    gaps.append(study.study_id)
                if "NON_COMPARABLE" in statuses:
                    non_comparable.append(study.study_id)
            result[question_id] = {
                "question_id": question_id,
                "study_count": len(self.corpus.records),
                "row_count": row_count,
                "covered_study_ids": tuple(covered),
                "gap_study_ids": tuple(gaps),
                "blocked_study_ids": tuple(blocked),
                "non_comparable_study_ids": tuple(non_comparable),
                "coverage_ratio": len(covered) / len(self.corpus.records),
            }
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_digest": self.scope.scope_digest,
            "rows": [row.to_dict() for row in self.rows],
            "coverage": _json_copy(self.coverage(), "MATRIX_INVALID"),
        }


@dataclass(frozen=True, slots=True)
class ResearchLoop:
    """Product-use composition for the bounded Research Loop slice."""

    scope: ReviewScope
    corpus: Corpus
    matrix: EvidenceMatrix

    def __post_init__(self) -> None:
        if self.matrix.scope != self.scope or self.matrix.corpus != self.corpus:
            _fail("RESEARCH_LOOP_INVALID", "matrix must bind the same scope and corpus")

    @classmethod
    def from_records(
        cls,
        *,
        scope: ReviewScope,
        corpus: Corpus | Iterable[CorpusStudy | Mapping[str, Any]],
        evidence: Iterable[EvidenceRecord | Mapping[str, Any]] | None = None,
    ) -> ResearchLoop:
        normalized_corpus = Corpus.from_records(corpus)
        matrix = EvidenceMatrix.from_records(
            scope=scope,
            corpus=normalized_corpus,
            evidence=evidence,
        )
        return cls(scope=scope, corpus=normalized_corpus, matrix=matrix)

    def view(self, **filters: Any) -> tuple[EvidenceRecord, ...]:
        return self.matrix.view(**filters)

    def gaps(self, **filters: Any) -> tuple[EvidenceRecord, ...]:
        return self.matrix.gaps(**filters)

    def provenance_for(self, study_id: str, question_id: str) -> tuple[dict[str, Any], ...]:
        return self.matrix.provenance_for(study_id, question_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.to_dict(),
            "corpus": self.corpus.to_dict(),
            "matrix": self.matrix.to_dict(),
        }


def build_research_loop(
    *,
    scope: ReviewScope,
    corpus: Corpus | Iterable[CorpusStudy | Mapping[str, Any]],
    evidence: Iterable[EvidenceRecord | Mapping[str, Any]] | None = None,
) -> ResearchLoop:
    """Build the import-free Research Loop product-use object."""

    return ResearchLoop.from_records(scope=scope, corpus=corpus, evidence=evidence)


__all__ = [
    "Corpus",
    "CorpusDocument",
    "CorpusStudy",
    "DOCUMENT_ROLES",
    "DuplicateGroup",
    "EVIDENCE_STATUSES",
    "EvidenceMatrix",
    "EvidenceRecord",
    "EvidenceRow",
    "ResearchLoop",
    "ResearchQuestion",
    "ResearchValidationError",
    "ReviewQuestion",
    "ReviewScope",
    "Scope",
    "SourceDocument",
    "build_research_loop",
]
