"""Deterministic, resumable study-batch orchestration.

The runner consumes provider outputs but never creates or edits them. It only
persists deterministic assembly, grounding, registration, and progress data.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl


REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_SCRIPTS = REPO_ROOT / "scripts" / "evidence"
if str(EVIDENCE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(EVIDENCE_SCRIPTS))

from assemble_evidence_candidate_from_atoms import (  # noqa: E402
    AssemblyError,
    assemble,
    validate_catalog,
    validate_schema,
    validate_sealed_job_binding,
)
from review_writer.project.vertical_review import (  # noqa: E402
    VerticalReviewError,
    register_study,
)
from review_writer.project.credit_ledger import record_credit_event  # noqa: E402
from scripts.evidence.validate_evidence_candidate import (  # noqa: E402
    validate as validate_evidence_candidate,
)


PrepareStudy = Callable[[str], dict[str, Any]]
CATALOG_SCHEMA = REPO_ROOT / "schemas/evidence/evidence_atom_catalog.v1.schema.json"
SEMANTIC_SCHEMA = REPO_ROOT / "schemas/evidence/evidence_atom_semantic_decision.v1.schema.json"
CANDIDATE_SCHEMA = REPO_ROOT / "schemas/evidence/evidence_candidate.v2.schema.json"
REVIEWER_VERDICTS = frozenset({"SUPPORT", "REJECT", "AMBIGUOUS"})


class BatchRunnerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@contextmanager
def _project_batch_lock(project: Path) -> Iterator[None]:
    lock_path = project / ".run_batch.lock"
    with lock_path.open("a+b") as lock:
        if os.name == "nt":
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BatchRunnerError(
                    "BATCH_ALREADY_RUNNING",
                    "another batch run already holds the project lock",
                ) from exc
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BatchRunnerError(
                    "BATCH_ALREADY_RUNNING",
                    "another batch run already holds the project lock",
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == content:
        return
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
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_deterministic_json(path: Path, payload: object) -> None:
    content = _json_bytes(payload)
    if path.exists() and path.read_bytes() != content:
        raise BatchRunnerError(
            "DETERMINISTIC_OUTPUT_CONFLICT",
            f"existing deterministic output differs: {path.name}",
        )
    _atomic_write_bytes(path, content)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise BatchRunnerError("PROJECT_SNAPSHOT_INVALID", f"invalid row in {path.name}")
            rows.append(row)
    return rows


def _load_project_snapshot(project: Path) -> dict[str, Any]:
    """Reload the canonical project files; callers must not cache this between stages."""
    try:
        review_state = _load_json(project / "00_brief/review_state.json")
        cards = _load_jsonl(project / "01_evidence/evidence_cards.jsonl")
        projection = _load_jsonl(project / "02_claims/claim_projection.jsonl")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BatchRunnerError("PROJECT_SNAPSHOT_INVALID", "canonical project snapshot is unreadable") from exc
    project_id = review_state.get("project_id") if isinstance(review_state, dict) else None
    if not isinstance(project_id, str) or not project_id:
        raise BatchRunnerError("PROJECT_SNAPSHOT_INVALID", "canonical project_id is missing")
    registered = {
        row["study_id"]
        for row in cards
        if isinstance(row.get("study_id"), str) and row["study_id"]
    }
    return {
        "cards": cards,
        "project_id": project_id,
        "projection": projection,
        "registered_study_ids": registered,
        "review_state": review_state,
    }


def _study_state(
    study_id: str,
    stage: str,
    reason_code: str,
    *,
    last_completed_stage: str,
    project_id: str,
    **bindings: str,
) -> dict[str, str]:
    return {
        "last_completed_stage": last_completed_stage,
        "project_id": project_id,
        "reason_code": reason_code,
        "stage": stage,
        "study_id": study_id,
        **bindings,
    }


def _persist_study_state(study_root: Path, state: dict[str, str]) -> None:
    _atomic_write_bytes(study_root / "batch_state.json", _json_bytes(state))


def _persisted_bindings(study_root: Path, fields: tuple[str, ...]) -> dict[str, str]:
    try:
        state = _load_json(study_root / "batch_state.json")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(state, dict) or any(not isinstance(state.get(field), str) for field in fields):
        return {}
    return {field: state[field] for field in fields}


def _load_resume_state(study_root: Path) -> dict[str, Any] | None:
    state_path = study_root / "batch_state.json"
    if not state_path.is_file():
        return None
    try:
        state = _load_json(state_path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BatchRunnerError("RESUME_STATE_INVALID", "persisted study stage is unreadable") from exc
    if not isinstance(state, dict):
        raise BatchRunnerError("RESUME_STATE_INVALID", "persisted study stage must be an object")
    return state


def _resume_prepared_inputs(
    study_root: Path, state: dict[str, Any] | None
) -> dict[str, str] | None:
    if state is None or state.get("last_completed_stage") not in {
        "PREPARED",
        "CANDIDATE_ASSEMBLED",
    }:
        return None
    bound_files = {
        "sealed_job_sha256": study_root / "sealed_job.json",
        "atom_catalog_sha256": study_root / "atom_catalog.json",
    }
    if any(
        not path.is_file()
        or not isinstance(state.get(field), str)
        or _sha256_file(path) != state[field]
        for field, path in bound_files.items()
    ):
        raise BatchRunnerError(
            "RESUME_BINDING_INVALID",
            "persisted prepared stage no longer binds the sealed job and atom catalog",
        )
    return {field: state[field] for field in bound_files}


def _resume_r0_outputs(
    project: Path,
    study_root: Path,
    state: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]] | None:
    if state is None or state.get("last_completed_stage") != "R0_PASS":
        return None
    bound_files = {
        "atom_catalog_sha256": study_root / "atom_catalog.json",
        "semantic_sha256": study_root / "semantic_decisions.json",
        "candidate_sha256": study_root / "evidence_candidate.json",
        "r0_sha256": study_root / "r0_report.json",
    }
    if any(
        not path.is_file()
        or not isinstance(state.get(field), str)
        or _sha256_file(path) != state[field]
        for field, path in bound_files.items()
    ):
        raise BatchRunnerError(
            "RESUME_BINDING_INVALID",
            "persisted R0 stage no longer binds semantic and deterministic artifacts",
        )
    try:
        candidate = _load_json(bound_files["candidate_sha256"])
        r0_report = _load_json(bound_files["r0_sha256"])
        sealed_job = _load_json(study_root / "sealed_job.json")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BatchRunnerError("RESUME_BINDING_INVALID", "bound R0 artifacts are unreadable") from exc
    if (
        not isinstance(candidate, dict)
        or not isinstance(r0_report, dict)
        or r0_report.get("status") != "R0_PASS"
        or candidate.get("job_id") != r0_report.get("job_id")
        or candidate.get("job_id") != r0_report.get("candidate_job_id")
    ):
        raise BatchRunnerError("RESUME_BINDING_INVALID", "bound R0 artifacts are inconsistent")
    try:
        fresh_r0_report = validate_evidence_candidate(
            sealed_job,
            candidate,
            project,
            _load_json(CANDIDATE_SCHEMA),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise BatchRunnerError(
            "RESUME_BINDING_INVALID",
            "bound R0 artifacts cannot be freshly validated",
        ) from exc
    if fresh_r0_report != r0_report:
        raise BatchRunnerError(
            "RESUME_BINDING_INVALID",
            "fresh deterministic validation differs from the bound R0 report",
        )
    return candidate, r0_report, state


def _credits_payload(
    *,
    credits_before: int | None,
    credits_after: int | None,
    forecast_credits: int | float | None,
    previous: Any,
) -> dict[str, Any]:
    if (credits_before is None) != (credits_after is None):
        raise BatchRunnerError(
            "MEASURED_CREDITS_INCOMPLETE",
            "credits_before and credits_after must be provided together",
        )
    previous = previous if isinstance(previous, dict) else {}
    measured = previous.get("measured")
    if measured is not None:
        if (
            not isinstance(measured, dict)
            or any(
                isinstance(measured.get(field), bool) or not isinstance(measured.get(field), int)
                for field in ("before", "after", "consumed")
            )
            or measured["before"] < 0
            or measured["after"] < 0
            or measured["after"] > measured["before"]
            or measured["consumed"] != measured["before"] - measured["after"]
        ):
            raise BatchRunnerError(
                "MEASURED_CREDITS_INVALID", "stored measured credits are invalid"
            )
    if credits_before is not None and credits_after is not None:
        if (
            isinstance(credits_before, bool)
            or isinstance(credits_after, bool)
            or not isinstance(credits_before, int)
            or not isinstance(credits_after, int)
            or credits_before < 0
            or credits_after < 0
            or credits_after > credits_before
            or (measured is not None and credits_before != measured["after"])
        ):
            raise BatchRunnerError("MEASURED_CREDITS_INVALID", "measured credit values are invalid")
        measured = {
            "after": credits_after,
            "before": measured["before"] if measured is not None else credits_before,
            "consumed": (
                measured["before"] if measured is not None else credits_before
            )
            - credits_after,
        }
    forecast = previous.get("forecast")
    previous_forecast = forecast.get("estimated_credits") if isinstance(forecast, dict) else None
    if forecast is not None and (
        isinstance(previous_forecast, bool)
        or not isinstance(previous_forecast, (int, float))
        or not math.isfinite(previous_forecast)
        or previous_forecast < 0
    ):
        raise BatchRunnerError("FORECAST_CREDITS_INVALID", "stored forecast credits are invalid")
    if forecast_credits is not None:
        if (
            isinstance(forecast_credits, bool)
            or not isinstance(forecast_credits, (int, float))
            or not math.isfinite(forecast_credits)
            or forecast_credits < 0
        ):
            raise BatchRunnerError("FORECAST_CREDITS_INVALID", "forecast credits are invalid")
        forecast = {"estimated_credits": forecast_credits}
    return {"forecast": forecast, "measured": measured}


def _reviewer_targets(candidate: dict[str, Any], reviewer: Any) -> None:
    expected = [
        row.get("reaction_unit_id") for row in candidate.get("reaction_units", [])
    ] + [row.get("claim_id") for row in candidate.get("claims", [])]
    findings = reviewer.get("findings") if isinstance(reviewer, dict) else None
    observed = [row.get("target_id") for row in findings] if isinstance(findings, list) and all(
        isinstance(row, dict) for row in findings
    ) else []
    if (
        not expected
        or any(not isinstance(value, str) or not value for value in expected)
        or len(expected) != len(set(expected))
        or len(observed) != len(set(observed))
        or set(observed) != set(expected)
    ):
        raise BatchRunnerError(
            "REVIEWER_TARGETS_INVALID",
            "reviewer findings must exactly cover candidate reaction units and claims",
        )


def _validate_reviewer_binding(
    candidate: dict[str, Any], reviewer: Any, candidate_sha256: str
) -> None:
    if (
        not isinstance(reviewer, dict)
        or reviewer.get("job_id") != candidate.get("job_id")
        or reviewer.get("study_id") != candidate.get("study_id")
    ):
        raise BatchRunnerError(
            "REVIEWER_BINDING_INVALID",
            "reviewer output must bind the current candidate job and study",
        )
    if reviewer.get("verdict") not in REVIEWER_VERDICTS:
        raise BatchRunnerError(
            "REVIEWER_VERDICT_INVALID",
            "reviewer verdict must be SUPPORT, REJECT, or AMBIGUOUS",
        )
    if reviewer.get("candidate_sha256") != candidate_sha256:
        raise BatchRunnerError(
            "REVIEWER_CANDIDATE_BINDING_INVALID",
            "reviewer output must bind the current candidate content",
        )
    _reviewer_targets(candidate, reviewer)


def _assemble_candidate(project: Path, study_root: Path) -> dict[str, Any]:
    job_path = study_root / "sealed_job.json"
    catalog_path = study_root / "atom_catalog.json"
    semantic_path = study_root / "semantic_decisions.json"
    job = _load_json(job_path)
    validate_sealed_job_binding(job)
    catalog = _load_json(catalog_path)
    semantic = _load_json(semantic_path)
    validate_schema(catalog, _load_json(CATALOG_SCHEMA), "CATALOG_SCHEMA_INVALID")
    validate_schema(semantic, _load_json(SEMANTIC_SCHEMA), "SEMANTIC_SCHEMA_INVALID")
    atoms = validate_catalog(job, job_path, catalog, project, source_pdfs={}, renderer=None)
    candidate = assemble(job, catalog, semantic, atoms)
    validate_schema(candidate, _load_json(CANDIDATE_SCHEMA), "CANDIDATE_SCHEMA_INVALID")
    return candidate


def _run_study(project: Path, study_id: str, prepare_study: PrepareStudy) -> dict[str, str]:
    study_root = project / "01_evidence" / study_id
    snapshot = _load_project_snapshot(project)
    project_id = snapshot["project_id"]
    if study_id in snapshot["registered_study_ids"]:
        state = _study_state(
            study_id,
            "REGISTERED",
            "STUDY_REGISTERED",
            last_completed_stage="REGISTERED",
            project_id=project_id,
        )
        _persist_study_state(study_root, state)
        return state
    completed_stage = "NONE"
    prepared_bindings: dict[str, str] = {}
    r0_bindings: dict[str, str] = {}
    try:
        persisted_state = _load_resume_state(study_root)
        if persisted_state is not None:
            persisted_stage = persisted_state.get("last_completed_stage")
            if persisted_stage in {"PREPARED", "CANDIDATE_ASSEMBLED", "R0_PASS"}:
                completed_stage = persisted_stage
            prepared_bindings = {
                field: persisted_state[field]
                for field in ("sealed_job_sha256", "atom_catalog_sha256")
                if isinstance(persisted_state.get(field), str)
            }
            r0_bindings = {
                field: persisted_state[field]
                for field in (
                    "atom_catalog_sha256",
                    "candidate_sha256",
                    "r0_sha256",
                    "semantic_sha256",
                )
                if isinstance(persisted_state.get(field), str)
            }
            if (
                persisted_state.get("reason_code") == "RESUME_BINDING_INVALID"
                or persisted_state.get("project_id") != project_id
                or persisted_state.get("study_id") != study_id
            ):
                raise BatchRunnerError(
                    "RESUME_BINDING_INVALID",
                    "persisted study stage belongs to different canonical inputs",
                )
        try:
            resumed = _resume_r0_outputs(project, study_root, persisted_state)
        except BatchRunnerError as exc:
            if exc.code == "RESUME_BINDING_INVALID":
                completed_stage = "R0_PASS"
                r0_bindings = _persisted_bindings(
                    study_root,
                    (
                        "atom_catalog_sha256",
                        "candidate_sha256",
                        "r0_sha256",
                        "semantic_sha256",
                    ),
                )
            raise
        if resumed is None:
            try:
                prepared_bindings = _resume_prepared_inputs(study_root, persisted_state)
            except BatchRunnerError as exc:
                if exc.code == "RESUME_BINDING_INVALID":
                    completed_stage = (
                        "CANDIDATE_ASSEMBLED"
                        if persisted_state
                        and persisted_state.get("last_completed_stage") == "CANDIDATE_ASSEMBLED"
                        else "PREPARED"
                    )
                    prepared_bindings = _persisted_bindings(
                        study_root,
                        ("sealed_job_sha256", "atom_catalog_sha256"),
                    )
                raise
            if prepared_bindings is None:
                prepared = prepare_study(study_id)
                if not isinstance(prepared, dict) or prepared.get("status") != "READY":
                    reason = prepared.get("reason_code") if isinstance(prepared, dict) else None
                    state = _study_state(
                        study_id,
                        "BLOCKED",
                        reason if isinstance(reason, str) and reason else "PREPARE_NOT_READY",
                        last_completed_stage="NONE",
                        project_id=project_id,
                    )
                    _persist_study_state(study_root, state)
                    return state
                if not (study_root / "sealed_job.json").is_file() or not (
                    study_root / "atom_catalog.json"
                ).is_file():
                    state = _study_state(
                        study_id,
                        "BLOCKED",
                        "PREPARE_OUTPUT_MISSING",
                        last_completed_stage="NONE",
                        project_id=project_id,
                    )
                    _persist_study_state(study_root, state)
                    return state
                prepared_bindings = {
                    "sealed_job_sha256": _sha256_file(study_root / "sealed_job.json"),
                    "atom_catalog_sha256": _sha256_file(study_root / "atom_catalog.json"),
                }
            completed_stage = "PREPARED"
            prepared_state = _study_state(
                study_id,
                "PREPARED",
                "PRE_PROVIDER_PACKET_READY",
                last_completed_stage="PREPARED",
                project_id=project_id,
                **prepared_bindings,
            )
            _persist_study_state(study_root, prepared_state)
            if not (study_root / "semantic_decisions.json").is_file():
                waiting = _study_state(
                    study_id,
                    "WAITING_FOR_PROVIDER",
                    "SEMANTIC_OUTPUT_MISSING",
                    last_completed_stage="PREPARED",
                    project_id=project_id,
                    **prepared_bindings,
                )
                _persist_study_state(study_root, waiting)
                return waiting

            _load_project_snapshot(project)
            candidate = _assemble_candidate(project, study_root)
            _write_deterministic_json(study_root / "evidence_candidate.json", candidate)
            completed_stage = "CANDIDATE_ASSEMBLED"
            _persist_study_state(
                study_root,
                _study_state(
                    study_id,
                    "CANDIDATE_ASSEMBLED",
                    "DETERMINISTIC_ASSEMBLY_COMPLETE",
                    last_completed_stage="CANDIDATE_ASSEMBLED",
                    project_id=project_id,
                    **prepared_bindings,
                ),
            )

            _load_project_snapshot(project)
            r0_report = validate_evidence_candidate(
                _load_json(study_root / "sealed_job.json"),
                candidate,
                project,
                _load_json(CANDIDATE_SCHEMA),
            )
            _write_deterministic_json(study_root / "r0_report.json", r0_report)
            if r0_report.get("status") != "R0_PASS":
                state = _study_state(
                    study_id,
                    "BLOCKED",
                    str(r0_report.get("status") or "R0_REJECTED"),
                    last_completed_stage="CANDIDATE_ASSEMBLED",
                    project_id=project_id,
                    **prepared_bindings,
                )
                _persist_study_state(study_root, state)
                return state
            completed_stage = "R0_PASS"
            r0_bindings = {
                "atom_catalog_sha256": _sha256_file(study_root / "atom_catalog.json"),
                "candidate_sha256": _sha256_file(study_root / "evidence_candidate.json"),
                "r0_sha256": _sha256_file(study_root / "r0_report.json"),
                "semantic_sha256": _sha256_file(study_root / "semantic_decisions.json"),
            }
            _persist_study_state(
                study_root,
                _study_state(
                    study_id,
                    "R0_PASS",
                    "FRESH_R0_PASS",
                    last_completed_stage="R0_PASS",
                    project_id=project_id,
                    **r0_bindings,
                ),
            )
            if not (study_root / "adversarial_verdict.json").is_file():
                waiting = _study_state(
                    study_id,
                    "WAITING_FOR_PROVIDER",
                    "REVIEWER_OUTPUT_MISSING",
                    last_completed_stage="R0_PASS",
                    project_id=project_id,
                    **r0_bindings,
                )
                _persist_study_state(study_root, waiting)
                return waiting
        else:
            candidate, r0_report, persisted = resumed
            completed_stage = "R0_PASS"
            r0_bindings = {
                field: persisted[field]
                for field in (
                    "atom_catalog_sha256",
                    "candidate_sha256",
                    "r0_sha256",
                    "semantic_sha256",
                )
            }
            if not (study_root / "adversarial_verdict.json").is_file():
                waiting = _study_state(
                    study_id,
                    "WAITING_FOR_PROVIDER",
                    "REVIEWER_OUTPUT_MISSING",
                    last_completed_stage="R0_PASS",
                    project_id=project_id,
                    **r0_bindings,
                )
                _persist_study_state(study_root, waiting)
                return waiting

        _load_project_snapshot(project)
        reviewer = _load_json(study_root / "adversarial_verdict.json")
        _validate_reviewer_binding(
            candidate,
            reviewer,
            _sha256_file(study_root / "evidence_candidate.json"),
        )

        _load_project_snapshot(project)
        register_study(project, candidate, r0_report, reviewer)
        registered = _study_state(
            study_id,
            "REGISTERED",
            "STUDY_REGISTERED",
            last_completed_stage="REGISTERED",
            project_id=project_id,
        )
        _persist_study_state(study_root, registered)
        return registered
    except BatchRunnerError as exc:
        state = _study_state(
            study_id,
            "BLOCKED",
            exc.code,
            last_completed_stage=completed_stage,
            project_id=project_id,
            **(
                r0_bindings
                if completed_stage == "R0_PASS"
                else prepared_bindings
                if completed_stage in {"PREPARED", "CANDIDATE_ASSEMBLED"}
                else {}
            ),
        )
    except AssemblyError as exc:
        state = _study_state(
            study_id,
            "BLOCKED",
            exc.code,
            last_completed_stage=completed_stage,
            project_id=project_id,
            **(
                r0_bindings
                if completed_stage == "R0_PASS"
                else prepared_bindings
                if completed_stage in {"PREPARED", "CANDIDATE_ASSEMBLED"}
                else {}
            ),
        )
    except VerticalReviewError as exc:
        state = _study_state(
            study_id,
            "BLOCKED",
            exc.code,
            last_completed_stage=completed_stage,
            project_id=project_id,
            **(
                r0_bindings
                if completed_stage == "R0_PASS"
                else prepared_bindings
                if completed_stage in {"PREPARED", "CANDIDATE_ASSEMBLED"}
                else {}
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        state = _study_state(
            study_id,
            "BLOCKED",
            "BATCH_INPUT_INVALID",
            last_completed_stage=completed_stage,
            project_id=project_id,
            **(
                r0_bindings
                if completed_stage == "R0_PASS"
                else prepared_bindings
                if completed_stage in {"PREPARED", "CANDIDATE_ASSEMBLED"}
                else {}
            ),
        )
    _persist_study_state(study_root, state)
    return state


def run_batch(
    project: Path,
    study_ids: Sequence[str],
    *,
    prepare_study: PrepareStudy,
    credits_before: int | None = None,
    credits_after: int | None = None,
    forecast_credits: int | float | None = None,
) -> dict[str, Any]:
    """Advance each study through deterministic stages until provider input is needed."""
    project_path = Path(project).resolve()
    identifiers = list(study_ids)
    if (
        not identifiers
        or len(identifiers) != len(set(identifiers))
        or any(
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            for value in identifiers
        )
    ):
        raise BatchRunnerError("STUDY_IDS_INVALID", "study IDs must be unique safe names")
    with _project_batch_lock(project_path):
        progress_path = project_path / "01_evidence/batch_progress.json"
        previous_progress = _load_json(progress_path) if progress_path.is_file() else {}
        credits = _credits_payload(
            credits_before=credits_before,
            credits_after=credits_after,
            forecast_credits=forecast_credits,
            previous=previous_progress.get("credits"),
        )
        current_studies = [
            _run_study(project_path, study_id, prepare_study) for study_id in identifiers
        ]
        studies_by_id = {
            row["study_id"]: row
            for row in previous_progress.get("studies", [])
            if isinstance(row, dict) and isinstance(row.get("study_id"), str)
        }
        studies_by_id.update({row["study_id"]: row for row in current_studies})
        studies = [studies_by_id[study_id] for study_id in sorted(studies_by_id)]
        stages = {row["stage"] for row in studies}
        status = (
            "BLOCKED"
            if "BLOCKED" in stages
            else "WAITING_FOR_PROVIDER"
            if "WAITING_FOR_PROVIDER" in stages
            else "COMPLETE"
        )
        summary = {
            "command": "run-batch",
            "credits": credits,
            "status": status,
            "studies": studies,
        }
        if credits_before is not None and credits_after is not None:
            record_credit_event(
                project_path,
                stage="run_batch",
                before=credits_before,
                after=credits_after,
                source="vertical_review_cli",
                study_ids=identifiers,
                forecast=(
                    credits["forecast"]["estimated_credits"]
                    if isinstance(credits.get("forecast"), dict)
                    else None
                ),
            )
        _atomic_write_bytes(
            progress_path,
            _json_bytes(summary),
        )
        return summary
