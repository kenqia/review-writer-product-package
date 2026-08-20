"""Canonical internal Generator Agent/session adapter.

The adapter is intentionally small: it owns the Agent actor and session
decision loop, while section-draft generation remains owned by
``project.manuscript_v2`` and human decisions remain owned by Dashboard's
existing draft seam.  Runtime state is stored inside the same immutable
VersionContext snapshot as the project current; no parallel session store is
created.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from review_writer.product_foundation import ProductFoundationError, VersionContext
from review_writer.product_foundation.contracts import validate_identifier
from review_writer.product_foundation.project_root import resolve_project_root
from review_writer.project.manuscript_v2 import (
    ManuscriptV2Error,
    build_manuscript_workspace,
    generate_section_draft_v2,
    register_section_draft,
)
from review_writer.project.paper_evidence_store import (
    PaperEvidenceStoreError,
    project_write_lock,
)
from review_writer.project.source_truth import canonical_digest


RUNTIME_SCHEMA = "review-writer.generator-runtime.v1"
RUNTIME_KEY = "generator_runtime"
AGENT_ACTOR_TYPE = "generator_agent"
HUMAN_ACTION_REQUIRED = "HUMAN_ACTION_REQUIRED"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LOCK_PATH = ".paper_evidence.lock"
_DRAFT_PATH = Path("04_manuscript/section_drafts.jsonl")


class GeneratorRuntimeError(ValueError):
    """Stable fail-closed error for the internal Generator runtime."""

    def __init__(self, code: str, *, tool_code: str | None = None) -> None:
        self.code = code
        self.tool_code = tool_code
        super().__init__(code)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_text(value: object, code: str, *, strip: bool = True) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GeneratorRuntimeError(code)
    return value.strip() if strip else value


def _identifier(value: object, code: str) -> str:
    try:
        return validate_identifier(value, field=code.lower())
    except ProductFoundationError as exc:
        raise GeneratorRuntimeError(code) from exc


def _registered_project_root(explicit_project_root: str | Path) -> Path:
    try:
        root = resolve_project_root(explicit_project_root)
    except ProductFoundationError as exc:
        raise GeneratorRuntimeError("PROJECT_ROOT_INVALID") from exc

    state_path = root / "00_brief" / "review_state.json"
    if state_path.is_symlink() or not state_path.is_file():
        raise GeneratorRuntimeError("PROJECT_NOT_REGISTERED")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GeneratorRuntimeError("PROJECT_REGISTRATION_INVALID") from exc
    if not isinstance(state, dict) or state.get("project_id") != root.name:
        raise GeneratorRuntimeError("PROJECT_REGISTRATION_INVALID")

    lock_path = root / _LOCK_PATH
    try:
        if (
            lock_path.is_symlink()
            or not lock_path.is_file()
            or lock_path.stat().st_size <= 0
        ):
            raise GeneratorRuntimeError("PROJECT_WRITE_LOCK_UNINITIALIZED")
    except OSError as exc:
        raise GeneratorRuntimeError("PROJECT_WRITE_LOCK_UNINITIALIZED") from exc
    return root


def _load_current(root: Path) -> tuple[VersionContext, Any, Any]:
    try:
        context = VersionContext.load(root)
        state = context.state()
        current = context.view_version(state.current_version_id)
    except (OSError, ProductFoundationError, TypeError, ValueError) as exc:
        raise GeneratorRuntimeError("VERSION_CONTEXT_INVALID") from exc
    if (
        state.project_id != root.name
        or not current.is_current
        or not current.is_active_head
        or not current.can_write
        or current.snapshot.get("currentness") != "current"
    ):
        raise GeneratorRuntimeError("VERSION_CONTEXT_INVALID")
    return context, state, current


def _check_expected(
    state: Any,
    *,
    expected_revision: int | None,
    expected_head_id: str | None,
) -> None:
    if expected_revision is not None and (
        type(expected_revision) is not int or expected_revision < 0
    ):
        raise GeneratorRuntimeError("GENERATOR_VERSION_CONFLICT")
    if expected_head_id is not None:
        try:
            expected_head_id = validate_identifier(
                expected_head_id,
                field="expected_head_id",
            )
        except ProductFoundationError as exc:
            raise GeneratorRuntimeError("GENERATOR_VERSION_CONFLICT") from exc
    if (expected_revision is not None and expected_revision != state.revision) or (
        expected_head_id is not None and expected_head_id != state.active_head_id
    ):
        raise GeneratorRuntimeError("GENERATOR_VERSION_CONFLICT")


def _binding(
    project_id: str,
    version_id: str,
    revision: int,
    digest: str,
    *,
    digest_scope: str,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "version_id": version_id,
        "revision": revision,
        "digest": digest,
        "digest_scope": digest_scope,
    }


def _event(
    event_type: str,
    *,
    run_id: str,
    input_binding: dict[str, Any],
    output_binding: dict[str, Any],
    details: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "event_id": _new_id("event"),
        "event_type": event_type,
        "actor_type": AGENT_ACTOR_TYPE,
        "run_id": run_id,
        "occurred_at": _now(),
        "input_binding": copy.deepcopy(input_binding),
        "output_binding": copy.deepcopy(output_binding),
        "details": copy.deepcopy(dict(details)),
    }


def _next_action(project_id: str) -> dict[str, str]:
    return {
        "project_id": project_id,
        "route": "/draft",
        "type": HUMAN_ACTION_REQUIRED,
    }


def _validate_start_request(request: object) -> dict[str, str]:
    if not isinstance(request, Mapping):
        raise GeneratorRuntimeError("GENERATOR_REQUEST_INVALID")
    allowed = {"session_id", "section_id", "heading", "body", "v2_addition"}
    if set(request) - allowed:
        raise GeneratorRuntimeError("GENERATOR_REQUEST_INVALID")
    session_id = request.get("session_id")
    if session_id is None:
        session_id = _new_id("generator-session")
    session_id = _identifier(session_id, "SESSION_ID_INVALID")
    section_id = _identifier(request.get("section_id"), "SECTION_ID_INVALID")
    heading = _require_text(request.get("heading"), "HEADING_INVALID")
    if heading != str(request.get("heading")) or "\n" in heading:
        raise GeneratorRuntimeError("HEADING_INVALID")
    body = _require_text(request.get("body"), "BODY_INVALID", strip=False)
    addition = _require_text(request.get("v2_addition"), "V2_ADDITION_INVALID")
    normalized = {
        "session_id": session_id,
        "section_id": section_id,
        "heading": heading,
        "body": body,
        "v2_addition": addition,
    }
    normalized["request_digest"] = canonical_digest(normalized)
    return normalized


def _runtime(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    value = snapshot.get(RUNTIME_KEY)
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("schema_version") != RUNTIME_SCHEMA:
        raise GeneratorRuntimeError("GENERATOR_RUNTIME_CORRUPT")
    if not isinstance(value.get("session_id"), str) or not isinstance(
        value.get("audit"), list
    ):
        raise GeneratorRuntimeError("GENERATOR_RUNTIME_CORRUPT")
    return copy.deepcopy(value)


def _capture_draft_state(root: Path) -> tuple[bool, bytes | None]:
    path = root / _DRAFT_PATH
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise GeneratorRuntimeError("SECTION_DRAFT_STATE_INVALID")
    if not path.exists():
        return False, None
    try:
        return True, path.read_bytes()
    except OSError as exc:
        raise GeneratorRuntimeError("SECTION_DRAFT_STATE_INVALID") from exc


def _restore_draft_state(root: Path, before: tuple[bool, bytes | None]) -> None:
    path = root / _DRAFT_PATH
    existed, payload = before
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise GeneratorRuntimeError("SECTION_DRAFT_STATE_INVALID")
    if not existed:
        path.unlink(missing_ok=True)
        return
    if payload is None:
        raise GeneratorRuntimeError("SECTION_DRAFT_STATE_INVALID")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    except OSError as exc:
        raise GeneratorRuntimeError("SECTION_DRAFT_ROLLBACK_FAILED") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _tool_error(exc: Exception) -> GeneratorRuntimeError:
    tool_code = getattr(exc, "code", None)
    if not isinstance(tool_code, str) or not tool_code:
        tool_code = "TOOL_FAILED"
    return GeneratorRuntimeError("GENERATOR_TOOL_FAILED", tool_code=tool_code)


def _response(
    runtime: Mapping[str, Any],
    *,
    state: Any,
    current: Any,
    run_id: str,
    write_mode: str,
) -> dict[str, Any]:
    candidate = runtime.get("candidate")
    if not isinstance(candidate, dict):
        raise GeneratorRuntimeError("GENERATOR_RUNTIME_CORRUPT")
    return {
        "status": HUMAN_ACTION_REQUIRED,
        "write_mode": write_mode,
        "session_id": runtime["session_id"],
        "run_id": run_id,
        "agent_action": runtime.get("last_action"),
        "candidate": copy.deepcopy(candidate),
        "current": {
            "project_id": state.project_id,
            "version_id": current.version_id,
            "revision": state.revision,
            "snapshot_digest": current.snapshot_digest,
        },
        "next_action": copy.deepcopy(runtime["next_action"]),
        "trace": {
            "event_count": len(runtime.get("audit", [])),
            "last_event_id": runtime.get("audit", [])[-1].get("event_id")
            if runtime.get("audit")
            else None,
        },
        "persistence": {
            "current_pointer": ".review-writer/version_context/current.json",
            "version_node": f".review-writer/version_context/versions/{current.version_id}.json",
        },
    }


def _publish_runtime(
    root: Path,
    *,
    context: VersionContext,
    state: Any,
    current: Any,
    runtime: dict[str, Any],
    version_id: str,
    draft_before: tuple[bool, bytes | None],
) -> tuple[Any, Any, Any]:
    snapshot = copy.deepcopy(dict(current.snapshot))
    snapshot[RUNTIME_KEY] = runtime
    try:
        with project_write_lock(root):
            latest_context, latest_state, latest_current = _load_current(root)
            if (
                latest_state.revision != state.revision
                or latest_state.current_version_id != current.version_id
            ):
                raise GeneratorRuntimeError("GENERATOR_VERSION_CONFLICT")
            node = latest_context.publish_active_head(
                snapshot,
                expected_head_id=latest_state.active_head_id,
                expected_revision=latest_state.revision,
                version_id=version_id,
            )
            return latest_context, latest_context.state(), node
    except Exception:
        try:
            with project_write_lock(root):
                _restore_draft_state(root, draft_before)
        except Exception as rollback_exc:
            raise GeneratorRuntimeError(
                "SECTION_DRAFT_ROLLBACK_FAILED"
            ) from rollback_exc
        raise


class GeneratorSession:
    """A restart-safe Agent actor for one project-scoped generator session."""

    def __init__(self, explicit_project_root: str | Path) -> None:
        self.root = _registered_project_root(explicit_project_root)

    def start(
        self,
        request: object,
        *,
        expected_revision: int | None = None,
        expected_head_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = _validate_start_request(request)
        context, state, current = _load_current(self.root)
        _check_expected(
            state,
            expected_revision=expected_revision,
            expected_head_id=expected_head_id,
        )
        existing = _runtime(current.snapshot)
        if existing is not None:
            existing_input = existing.get("input")
            if (
                existing.get("session_id") != normalized["session_id"]
                or not isinstance(existing_input, dict)
                or existing_input.get("request_digest") != normalized["request_digest"]
            ):
                raise GeneratorRuntimeError("GENERATOR_SESSION_CONFLICT")
            return _response(
                existing,
                state=state,
                current=current,
                run_id=str(existing.get("last_run_id") or "resume"),
                write_mode="NONE",
            )

        run_id = _new_id("generator-run")
        tool_payload = {
            "section_id": normalized["section_id"],
            "heading": normalized["heading"],
            "body": normalized["body"],
            "content_agent_result_digest": _sha256_text(normalized["body"]),
        }
        draft_before = _capture_draft_state(self.root)
        try:
            candidate = register_section_draft(self.root, tool_payload)
        except (ManuscriptV2Error, PaperEvidenceStoreError, OSError, ValueError) as exc:
            try:
                with project_write_lock(self.root):
                    _restore_draft_state(self.root, draft_before)
            except Exception as rollback_exc:
                raise GeneratorRuntimeError(
                    "SECTION_DRAFT_ROLLBACK_FAILED"
                ) from rollback_exc
            raise _tool_error(exc) from exc

        candidate_digest = candidate.get("draft_digest")
        if not isinstance(candidate_digest, str) or not _SHA256.fullmatch(
            candidate_digest
        ):
            try:
                with project_write_lock(self.root):
                    _restore_draft_state(self.root, draft_before)
            except Exception as rollback_exc:
                raise GeneratorRuntimeError(
                    "SECTION_DRAFT_ROLLBACK_FAILED"
                ) from rollback_exc
            raise GeneratorRuntimeError("GENERATOR_TOOL_RESULT_INVALID")

        new_version_id = _new_id("generator-v1")
        input_binding = _binding(
            state.project_id,
            current.version_id,
            state.revision,
            current.snapshot_digest,
            digest_scope="version_node",
        )
        output_binding = _binding(
            state.project_id,
            new_version_id,
            state.revision + 1,
            candidate_digest,
            digest_scope="draft_artifact",
        )
        next_action = _next_action(state.project_id)
        runtime = {
            "schema_version": RUNTIME_SCHEMA,
            "project_id": state.project_id,
            "session_id": normalized["session_id"],
            "phase": "v1",
            "status": HUMAN_ACTION_REQUIRED,
            "last_action": "GENERATE_CANDIDATE_V1",
            "last_run_id": run_id,
            "next_action": next_action,
            "input": {
                "section_id": normalized["section_id"],
                "heading": normalized["heading"],
                "v2_addition": normalized["v2_addition"],
                "request_digest": normalized["request_digest"],
            },
            "candidate": {
                "version": "v1",
                "section_id": normalized["section_id"],
                "draft_digest": candidate_digest,
                "body_sha256": _sha256_text(normalized["body"]),
                "status": candidate.get("status"),
                "generation_digest": tool_payload["content_agent_result_digest"],
            },
            "human_decision": None,
            "audit": [
                _event(
                    "agent_action",
                    run_id=run_id,
                    input_binding=input_binding,
                    output_binding=output_binding,
                    details={"action": "GENERATE_CANDIDATE_V1"},
                ),
                _event(
                    "tool_invocation",
                    run_id=run_id,
                    input_binding=input_binding,
                    output_binding=output_binding,
                    details={
                        "tool": "register_section_draft",
                        "input_digest": canonical_digest(tool_payload),
                    },
                ),
                _event(
                    "tool_result",
                    run_id=run_id,
                    input_binding=input_binding,
                    output_binding=output_binding,
                    details={
                        "status": "SUCCESS",
                        "result_digest": candidate_digest,
                        "section_id": normalized["section_id"],
                    },
                ),
                _event(
                    "human_action_required",
                    run_id=run_id,
                    input_binding=output_binding,
                    output_binding=output_binding,
                    details={"decision_owner": "Dashboard.draft"},
                ),
            ],
        }
        try:
            _, new_state, node = _publish_runtime(
                self.root,
                context=context,
                state=state,
                current=current,
                runtime=runtime,
                version_id=new_version_id,
                draft_before=draft_before,
            )
        except GeneratorRuntimeError:
            raise
        except ProductFoundationError as exc:
            raise GeneratorRuntimeError("GENERATOR_VERSION_CONFLICT") from exc
        return _response(
            runtime,
            state=new_state,
            current=node,
            run_id=run_id,
            write_mode="VERSION_CONTEXT",
        )

    def continue_session(
        self,
        session_id: str,
        *,
        expected_revision: int | None = None,
        expected_head_id: str | None = None,
    ) -> dict[str, Any]:
        session_id = _identifier(session_id, "SESSION_ID_INVALID")
        context, state, current = _load_current(self.root)
        _check_expected(
            state,
            expected_revision=expected_revision,
            expected_head_id=expected_head_id,
        )
        runtime = _runtime(current.snapshot)
        if runtime is None or runtime.get("session_id") != session_id:
            raise GeneratorRuntimeError("GENERATOR_SESSION_NOT_FOUND")
        if runtime.get("phase") == "v2":
            return _response(
                runtime,
                state=state,
                current=current,
                run_id=str(runtime.get("last_run_id") or "resume"),
                write_mode="NONE",
            )
        if runtime.get("phase") != "v1" or not isinstance(runtime.get("input"), dict):
            raise GeneratorRuntimeError("GENERATOR_RUNTIME_CORRUPT")

        try:
            workspace = build_manuscript_workspace(self.root)
        except (ManuscriptV2Error, OSError, ValueError) as exc:
            raise GeneratorRuntimeError("GENERATOR_DECISION_READ_FAILED") from exc
        sections = workspace.get("sections")
        if not isinstance(sections, list):
            raise GeneratorRuntimeError("GENERATOR_DECISION_READ_FAILED")
        section_id = runtime["input"].get("section_id")
        section = next(
            (
                row
                for row in sections
                if isinstance(row, dict) and row.get("section_id") == section_id
            ),
            None,
        )
        if section is None or section.get("status") == "stale":
            raise GeneratorRuntimeError("GENERATOR_DRAFT_STALE")
        decision = section.get("decision")
        if not isinstance(decision, dict) or decision.get("action") != "approve":
            return _response(
                runtime,
                state=state,
                current=current,
                run_id="human-action-pending",
                write_mode="NONE",
            )
        candidate_v1 = runtime.get("candidate")
        original_expression = decision.get("original_expression")
        if (
            not isinstance(candidate_v1, dict)
            or candidate_v1.get("version") != "v1"
            or not isinstance(original_expression, str)
            or candidate_v1.get("body_sha256") != _sha256_text(original_expression)
            or decision.get("bound_object_digest") != section.get("draft_digest")
        ):
            raise GeneratorRuntimeError("GENERATOR_DRAFT_STALE")

        run_id = _new_id("generator-run")
        input_binding = _binding(
            state.project_id,
            current.version_id,
            state.revision,
            current.snapshot_digest,
            digest_scope="version_node",
        )
        decision_digest = canonical_digest(decision)
        edited_body = section.get("body")
        if not isinstance(edited_body, str) or not edited_body.strip():
            raise GeneratorRuntimeError("GENERATOR_DRAFT_STALE")
        decision_binding = {
            "action": "approve",
            "decision_digest": decision_digest,
            "edited_body_sha256": _sha256_text(edited_body),
            "draft_digest": section.get("draft_digest"),
        }
        addition = runtime["input"].get("v2_addition")
        if not isinstance(addition, str) or not addition.strip():
            raise GeneratorRuntimeError("GENERATOR_RUNTIME_CORRUPT")
        draft_before = _capture_draft_state(self.root)
        try:
            candidate = generate_section_draft_v2(
                self.root,
                {
                    "section_id": section_id,
                    "body": addition,
                    "content_agent_result_digest": _sha256_text(addition),
                },
            )
        except (ManuscriptV2Error, PaperEvidenceStoreError, OSError, ValueError) as exc:
            try:
                with project_write_lock(self.root):
                    _restore_draft_state(self.root, draft_before)
            except Exception as rollback_exc:
                raise GeneratorRuntimeError(
                    "SECTION_DRAFT_ROLLBACK_FAILED"
                ) from rollback_exc
            raise _tool_error(exc) from exc

        candidate_digest = candidate.get("draft_digest")
        if not isinstance(candidate_digest, str) or not _SHA256.fullmatch(
            candidate_digest
        ):
            try:
                with project_write_lock(self.root):
                    _restore_draft_state(self.root, draft_before)
            except Exception as rollback_exc:
                raise GeneratorRuntimeError(
                    "SECTION_DRAFT_ROLLBACK_FAILED"
                ) from rollback_exc
            raise GeneratorRuntimeError("GENERATOR_TOOL_RESULT_INVALID")
        if not isinstance(candidate.get("body"), str) or not candidate[
            "body"
        ].startswith(edited_body.rstrip()):
            try:
                with project_write_lock(self.root):
                    _restore_draft_state(self.root, draft_before)
            except Exception as rollback_exc:
                raise GeneratorRuntimeError(
                    "SECTION_DRAFT_ROLLBACK_FAILED"
                ) from rollback_exc
            raise GeneratorRuntimeError("GENERATOR_MARKER_PRESERVATION_FAILED")

        new_version_id = _new_id("generator-v2")
        output_binding = _binding(
            state.project_id,
            new_version_id,
            state.revision + 1,
            candidate_digest,
            digest_scope="draft_artifact",
        )
        inherited_audit = runtime.get("audit")
        if not isinstance(inherited_audit, list):
            raise GeneratorRuntimeError("GENERATOR_RUNTIME_CORRUPT")
        runtime_v2 = copy.deepcopy(runtime)
        runtime_v2.update(
            {
                "phase": "v2",
                "status": HUMAN_ACTION_REQUIRED,
                "last_action": "GENERATE_CANDIDATE_V2",
                "last_run_id": run_id,
                "candidate": {
                    "version": "v2",
                    "section_id": section_id,
                    "draft_digest": candidate_digest,
                    "status": candidate.get("status"),
                    "generation_digest": _sha256_text(addition),
                },
                "human_decision": decision_binding,
            }
        )
        runtime_v2["audit"] = [
            *inherited_audit,
            _event(
                "resume",
                run_id=run_id,
                input_binding=input_binding,
                output_binding=output_binding,
                details={"session_id": session_id, "decision_digest": decision_digest},
            ),
            _event(
                "human_decision",
                run_id=run_id,
                input_binding=input_binding,
                output_binding=output_binding,
                details=decision_binding,
            ),
            _event(
                "agent_action",
                run_id=run_id,
                input_binding=input_binding,
                output_binding=output_binding,
                details={"action": "GENERATE_CANDIDATE_V2"},
            ),
            _event(
                "tool_invocation",
                run_id=run_id,
                input_binding=input_binding,
                output_binding=output_binding,
                details={
                    "tool": "generate_section_draft_v2",
                    "input_digest": canonical_digest(
                        {
                            "section_id": section_id,
                            "body": addition,
                            "content_agent_result_digest": _sha256_text(addition),
                        }
                    ),
                },
            ),
            _event(
                "tool_result",
                run_id=run_id,
                input_binding=input_binding,
                output_binding=output_binding,
                details={
                    "status": "SUCCESS",
                    "result_digest": candidate_digest,
                    "edited_body_sha256": _sha256_text(edited_body),
                },
            ),
            _event(
                "human_action_required",
                run_id=run_id,
                input_binding=output_binding,
                output_binding=output_binding,
                details={"decision_owner": "Dashboard.draft"},
            ),
        ]
        try:
            _, new_state, node = _publish_runtime(
                self.root,
                context=context,
                state=state,
                current=current,
                runtime=runtime_v2,
                version_id=new_version_id,
                draft_before=draft_before,
            )
        except GeneratorRuntimeError:
            raise
        except ProductFoundationError as exc:
            raise GeneratorRuntimeError("GENERATOR_VERSION_CONFLICT") from exc
        return _response(
            runtime_v2,
            state=new_state,
            current=node,
            run_id=run_id,
            write_mode="VERSION_CONTEXT",
        )
