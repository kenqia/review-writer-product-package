"""Public Agent-first intake adapter for one explicit review project root.

The adapter is intentionally thin. Fresh projects are created only by the
existing ``FreshAgentBootstrap``; existing projects are read only from their
canonical ``VersionContext`` and source archive. No public-entry state is
persisted here.
"""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen
from urllib.parse import urlparse

from review_writer.delivery.project_release import (
    build_project_release,
    new_route_release_docx_is_current,
)
from review_writer.product_foundation import ProductFoundationError, VersionContext
from review_writer.project.manuscript_v2 import manuscript_state
from review_writer.product_foundation.project_root import resolve_project_root
from review_writer.project.paper_evidence import paper_evidence_state
from . import fresh_bootstrap, local_pdf_parse
from .generator_runtime import RUNTIME_KEY, RUNTIME_SCHEMA, GeneratorSession

# Keep the public adapter's seam explicit while reusing the existing fresh
# bootstrap implementation.  Tests and callers can patch/inspect this seam
# without introducing a second bootstrap implementation.
FreshAgentBootstrap = fresh_bootstrap.FreshAgentBootstrap
FreshAgentBootstrapError = fresh_bootstrap.FreshAgentBootstrapError


_SAFE_TEXT_MAX = 20_000
_SAFE_URL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_RELEASE_DOCX_PATHS = (
    ("EXPERT_REVIEWED_RELEASE", Path("05_release/expert_reviewed_release.docx")),
    ("SELF_REVIEWED_DRAFT", Path("05_release/self_reviewed_draft.docx")),
)
_RELEASE_ARTIFACT_PATHS = tuple(
    path
    for _level, docx_path in _RELEASE_DOCX_PATHS
    for path in (
        docx_path,
        docx_path.with_suffix(".md"),
    )
) + (
    Path("05_release/release_snapshot.json"),
    Path("05_release/quality_report.json"),
)
_PDF_EVIDENCE_BRIDGE_HOLD_CODES = frozenset(
    {
        "PARSE_QUALITY_MISSING",
        "PARSE_QUALITY_REVIEW_REQUIRED",
        "PARSE_QUALITY_STALE",
        "PARSE_PDF_LOCATOR_ONLY",
    }
)


class PublicReviewEntryError(ValueError):
    """Stable, non-secret error returned by the public Agent entry."""

    def __init__(
        self,
        code: str,
        *,
        category: str = "INVALID_INPUT",
        write_mode: str = "zero_write",
        current_unchanged: bool = True,
    ) -> None:
        self.code = code
        self.category = category
        self.write_mode = write_mode
        self.current_unchanged = current_unchanged
        super().__init__(code)


def _error(
    code: str,
    *,
    category: str = "INVALID_INPUT",
    write_mode: str = "zero_write",
) -> PublicReviewEntryError:
    return PublicReviewEntryError(
        code,
        category=category,
        write_mode=write_mode,
        current_unchanged=True,
    )


def _required_text(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > _SAFE_TEXT_MAX
        or "\x00" in value
    ):
        raise _error(code)
    return value


def _optional_text(value: object, code: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, code)


def _explicit_root(value: object) -> tuple[Path, bool]:
    try:
        supplied = Path(value)
    except (TypeError, ValueError) as exc:
        raise _error("PROJECT_ROOT_INVALID") from exc
    if (
        not supplied.is_absolute()
        or not supplied.name
        or (os.path.lexists(supplied) and supplied.is_symlink())
    ):
        raise _error("PROJECT_ROOT_INVALID")
    if os.path.lexists(supplied):
        if not supplied.is_dir():
            raise _error("PROJECT_ROOT_INVALID")
        try:
            resolved = resolve_project_root(supplied)
        except (OSError, ProductFoundationError, TypeError, ValueError) as exc:
            raise _error("PROJECT_ROOT_INVALID") from exc
        try:
            return resolved, not any(resolved.iterdir())
        except OSError as exc:
            raise _error("PROJECT_ROOT_INVALID") from exc
    try:
        parent = supplied.parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error("PROJECT_ROOT_INVALID") from exc
    if not parent.is_dir() or parent.is_symlink():
        raise _error("PROJECT_ROOT_INVALID")
    return parent / supplied.name, True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise _error("AUTHORIZED_PDF_INVALID") from exc
    return digest.hexdigest()


def _authorized_source_set(pdfs: tuple[Path, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, path in enumerate(pdfs, start=1):
        try:
            digest = _sha256(path)
            size = path.stat().st_size
        except OSError as exc:
            raise _error("AUTHORIZED_PDF_INVALID") from exc
        rows.append(
            {
                "member_id": f"MEMBER-{index:04d}",
                "name": path.name,
                "sha256": digest,
                "size_bytes": size,
                "download_id": f"UPLOAD-{digest[:20]}",
                "source_id": f"UPLOAD-{digest[:20]}",
                "study_id": f"UPLOAD-{digest[:20]}",
            }
        )
    return rows


def _same_source_set(expected: object, observed: list[dict[str, Any]]) -> bool:
    if not isinstance(expected, list) or len(expected) != len(observed):
        return False
    fields = (
        "member_id",
        "name",
        "sha256",
        "size_bytes",
        "download_id",
        "source_id",
        "study_id",
    )
    return all(
        isinstance(row, dict)
        and all(row.get(field) == actual.get(field) for field in fields)
        for row, actual in zip(expected, observed, strict=True)
    )


def _validate_resume_source(
    project: Path,
    authorized_pdfs: tuple[Path, ...],
    snapshot: dict[str, Any],
) -> None:
    bootstrap = snapshot.get("agent_bootstrap")
    if not isinstance(bootstrap, dict):
        raise _error("AUTHORIZED_PDF_STALE", category="VERSION_CONFLICT")
    observed_source_set = _authorized_source_set(authorized_pdfs)
    if not _same_source_set(bootstrap.get("authorized_source_set"), observed_source_set):
        raise _error("AUTHORIZED_PDF_STALE", category="VERSION_CONFLICT")
    archive = project / fresh_bootstrap.SOURCE_ARCHIVE_RELATIVE
    if archive.is_symlink() or not archive.is_file():
        raise _error("AUTHORIZED_PDF_STALE", category="VERSION_CONFLICT")
    try:
        preflight = fresh_bootstrap._preflight_source_archive(archive, observed_source_set)
    except fresh_bootstrap.FreshAgentBootstrapError as exc:
        raise _error(exc.code, category="VERSION_CONFLICT") from exc
    stored_preflight = bootstrap.get("source_archive")
    if (
        not isinstance(stored_preflight, dict)
        or stored_preflight.get("archive_sha256") != preflight.get("archive_sha256")
        or stored_preflight.get("members") != preflight.get("members")
    ):
        raise _error("AUTHORIZED_PDF_STALE", category="VERSION_CONFLICT")


def _loopback_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in _SAFE_URL_HOSTS:
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    return value


def _dashboard_url(snapshot: dict[str, Any]) -> str | None:
    for owner_key in ("agent_bootstrap", "generator_runtime", "agent_parse"):
        owner = snapshot.get(owner_key)
        if not isinstance(owner, dict):
            continue
        direct = _loopback_url(owner.get("dashboard_url"))
        if direct is not None:
            return direct
        trace = owner.get("tool_trace")
        if isinstance(trace, list):
            for event in reversed(trace):
                if isinstance(event, dict):
                    found = _loopback_url(event.get("dashboard_url"))
                    if found is not None:
                        return found
    return None


def _dashboard_is_healthy(value: str) -> bool:
    safe_url = _loopback_url(value)
    if safe_url is None:
        return False
    try:
        with urlopen(f"{safe_url.rstrip('/')}/api/projects", timeout=0.2) as response:
            return response.status == 200
    except (OSError, URLError, ValueError):
        return False


def _resume_dashboard(
    project: Path, snapshot: dict[str, Any]
) -> tuple[str | None, int | None, str | None]:
    stored_url = _dashboard_url(snapshot)
    if stored_url is not None and _dashboard_is_healthy(stored_url):
        return stored_url, None, None
    try:
        started = fresh_bootstrap._start_dashboard(project.parent)
    except fresh_bootstrap.FreshAgentBootstrapError as exc:
        return None, None, exc.code
    except OSError:
        return None, None, "DASHBOARD_START_FAILED"
    if (
        not isinstance(started, tuple)
        or len(started) != 2
        or not isinstance(started[0], str)
        or not isinstance(started[1], int)
    ):
        return None, None, "DASHBOARD_RESULT_INVALID"
    dashboard_url, dashboard_pid = started
    safe_url = _loopback_url(dashboard_url)
    if safe_url is None:
        return None, None, "DASHBOARD_RESULT_INVALID"
    return safe_url, dashboard_pid, None


def _dashboard_hold(
    project: Path, current_payload: dict[str, Any], reason_code: str
) -> dict[str, Any]:
    return {
        "result": "RESUMED",
        "status": "HOLD",
        "reason_code": reason_code,
        "dashboard_url": None,
        "current": copy.deepcopy(current_payload),
        "revision": current_payload["revision"],
        "write_mode": "NONE",
        "next_action": {
            "project_id": project.name,
            "route": "/review",
            "type": "HOLD",
            "reason_code": reason_code,
        },
        "trace": {"event_count": 0},
    }


def _nested_status(
    snapshot: dict[str, Any],
) -> tuple[str, str | None, dict[str, Any] | None, Any]:
    for owner_key in ("generator_runtime", "agent_parse", "agent_bootstrap"):
        owner = snapshot.get(owner_key)
        if not isinstance(owner, dict):
            continue
        status = owner.get("status")
        reason = owner.get("reason_code")
        next_action = owner.get("next_action")
        if isinstance(status, str) and status.strip():
            return (
                status,
                reason if isinstance(reason, str) else None,
                next_action if isinstance(next_action, dict) else None,
                owner.get("tool_trace", owner.get("audit")),
            )
    return "RESUMED", None, None, None


def _parse_artifacts_exist(project: Path) -> bool:
    evidence = project / "01_evidence"
    return any(
        os.path.lexists(evidence / component)
        for component in local_pdf_parse._EVIDENCE_COMPONENTS
    )


def _source_mapping_complete(project: Path, snapshot: dict[str, Any]) -> bool:
    bootstrap = snapshot.get("agent_bootstrap")
    if (
        not isinstance(bootstrap, dict)
        or bootstrap.get("status") != fresh_bootstrap.HUMAN_ACTION_REQUIRED
    ):
        return False
    receipt = project / local_pdf_parse._RECEIPT
    return receipt.is_file() and not receipt.is_symlink()


def _current_payload(project: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    try:
        context = VersionContext.load(project)
        state = context.state()
        current = context.view_version(state.current_version_id)
    except (OSError, ProductFoundationError, TypeError, ValueError) as exc:
        raise _error("VERSION_CONTEXT_INVALID", category="PRECONDITION_FAILED") from exc
    if (
        state.project_id != project.name
        or not current.is_current
        or not current.is_active_head
        or not current.can_write
        or snapshot.get("currentness") != "current"
    ):
        raise _error("VERSION_CONTEXT_INVALID", category="PRECONDITION_FAILED")
    # Dashboard imports this package's generator runtime, so defer this reverse
    # dependency until resume validation actually needs it.
    from view.serve_review_dashboard import PublicProjectResumeError, _resume_artifact_refs

    try:
        _resume_artifact_refs(project, snapshot)
    except PublicProjectResumeError as exc:
        category = (
            "VERSION_CONFLICT" if exc.code == "VERSION_CONFLICT" else "PRECONDITION_FAILED"
        )
        raise _error(exc.code, category=category) from exc
    return {
        "project_id": state.project_id,
        "version_id": current.version_id,
        "revision": state.revision,
        "snapshot_digest": current.snapshot_digest,
    }


def _v2_human_approval(snapshot: object) -> bool:
    if not isinstance(snapshot, dict):
        return False
    runtime = snapshot.get(RUNTIME_KEY)
    candidate = runtime.get("candidate") if isinstance(runtime, dict) else None
    decision = runtime.get("human_decision") if isinstance(runtime, dict) else None
    return bool(
        isinstance(runtime, dict)
        and runtime.get("schema_version") == RUNTIME_SCHEMA
        and runtime.get("phase") == "v2"
        and isinstance(candidate, dict)
        and candidate.get("version") == "v2"
        and isinstance(decision, dict)
        and decision.get("actor_type") == "human_researcher"
    )


def _release_binding(project: Path) -> tuple[Any, Any, dict[str, Any]]:
    try:
        context = VersionContext.load(project)
        state = context.state()
        current = context.view_version(state.current_version_id)
    except (OSError, ProductFoundationError, TypeError, ValueError) as exc:
        raise _error("VERSION_CONTEXT_INVALID", category="PRECONDITION_FAILED") from exc
    if (
        state.project_id != project.name
        or not current.is_current
        or not current.is_active_head
        or not current.can_write
        or current.snapshot.get("currentness") != "current"
    ):
        raise _error("VERSION_CONTEXT_INVALID", category="PRECONDITION_FAILED")
    return state, current, {
        "version_id": current.version_id,
        "revision": state.revision,
        "snapshot_digest": current.snapshot_digest,
    }


def _release_artifact_state(project: Path) -> tuple[str, str | None, Path | None]:
    for release_level, relative in _RELEASE_DOCX_PATHS:
        candidate = project / relative
        if (
            candidate.is_file()
            and not candidate.is_symlink()
            and new_route_release_docx_is_current(candidate)
        ):
            return "current", release_level, candidate
    if any(os.path.lexists(project / relative) for relative in _RELEASE_ARTIFACT_PATHS):
        return "stale", None, None
    return "missing", None, None


def _release_relative_path(project: Path, value: object) -> str:
    try:
        candidate = Path(value)
        root = project.resolve(strict=True)
        resolved = (
            candidate if candidate.is_absolute() else root / candidate
        ).resolve(strict=False)
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _error("RELEASE_RESULT_INVALID", category="PRECONDITION_FAILED") from exc
    if not relative.parts or relative.parts[0] != "05_release":
        raise _error("RELEASE_RESULT_INVALID", category="PRECONDITION_FAILED")
    return relative.as_posix()


def _public_release(
    project: Path,
    release: dict[str, Any],
    binding: dict[str, Any],
    *,
    default_level: str | None = None,
) -> dict[str, Any]:
    status = release.get("status")
    release_level = release.get("release_level", default_level)
    if not isinstance(status, str) or not status or not isinstance(release_level, str):
        raise _error("RELEASE_RESULT_INVALID", category="PRECONDITION_FAILED")
    snapshot = release.get("snapshot")
    docx = release.get("docx")
    if snapshot is None or docx is None:
        raise _error("RELEASE_RESULT_INVALID", category="PRECONDITION_FAILED")
    return {
        "status": status,
        "release_level": release_level,
        "markdown_path": _release_relative_path(project, snapshot),
        "docx_path": _release_relative_path(project, docx),
        "version_context": copy.deepcopy(binding),
    }


def _continue_v2_release(project: Path) -> dict[str, Any] | None:
    _state, current, binding = _release_binding(project)
    snapshot = copy.deepcopy(dict(current.snapshot))
    if not _v2_human_approval(snapshot):
        return None
    authoritative = manuscript_state(project)
    if (
        not isinstance(authoritative, dict)
        or authoritative.get("workflow_can_continue") is not True
    ):
        return None
    artifact_state, release_level, docx = _release_artifact_state(project)
    if artifact_state == "current" and release_level is not None and docx is not None:
        return {
            "release_status": release_level,
            "release": {
                "status": release_level,
                "release_level": release_level,
                "markdown_path": docx.with_suffix(".md").relative_to(project).as_posix(),
                "docx_path": docx.relative_to(project).as_posix(),
                "version_context": copy.deepcopy(binding),
            },
        }
    if artifact_state == "stale":
        return {
            "release_status": "RELEASE_OUTDATED",
            "next_action": {
                "project_id": project.name,
                "route": "/final",
                "type": "REGENERATE_RELEASE",
                "reason_code": "RELEASE_OUTDATED",
            },
        }
    release = build_project_release(project)
    public_release = _public_release(project, release, binding, default_level="SELF_REVIEWED_DRAFT")
    return {
        "release_status": public_release["status"],
        "release": public_release,
    }


def _resume(project: Path, authorized_pdfs: tuple[Path, ...]) -> dict[str, Any]:
    try:
        context = VersionContext.load(project)
        state = context.state()
        current = context.view_version(state.current_version_id)
    except (OSError, ProductFoundationError, TypeError, ValueError) as exc:
        raise _error("VERSION_CONTEXT_INVALID", category="PRECONDITION_FAILED") from exc
    snapshot = copy.deepcopy(dict(current.snapshot))
    _validate_resume_source(project, authorized_pdfs, snapshot)
    current_payload = _current_payload(project, snapshot)
    dashboard_url, dashboard_pid, dashboard_failure = _resume_dashboard(project, snapshot)
    dashboard_fields: dict[str, Any] = {"dashboard_url": dashboard_url}
    if dashboard_pid is not None:
        dashboard_fields["dashboard_pid"] = dashboard_pid
    if _source_mapping_complete(project, snapshot) and not _parse_artifacts_exist(project):
        parsed = local_pdf_parse.parse_project_sources(
            project,
            expected_revision=state.revision,
            expected_head_id=state.active_head_id,
        )
        parsed_current = parsed.get("current") if isinstance(parsed, dict) else None
        if (
            not isinstance(parsed_current, dict)
            or not {"version_id", "revision", "snapshot_digest"}.issubset(parsed_current)
        ):
            raise _error("PARSE_RESULT_INVALID", category="PRECONDITION_FAILED")
        parsed_current = copy.deepcopy(parsed_current)
        parsed_current.setdefault("project_id", project.name)
        return {
            **copy.deepcopy(parsed),
            "result": "RESUMED",
            "current": parsed_current,
            "revision": parsed_current["revision"],
            "write_mode": "VERSION_CONTEXT",
            **dashboard_fields,
        }
    parse_owner = snapshot.get("agent_parse")
    session_id = parse_owner.get("session_id") if isinstance(parse_owner, dict) else None
    if _parse_artifacts_exist(project) and isinstance(session_id, str) and session_id:
        evidence = paper_evidence_state(project)
        if isinstance(evidence, dict) and evidence.get("workflow_can_continue") is True:
            continued = GeneratorSession(project).continue_session(
                session_id,
                expected_revision=state.revision,
                expected_head_id=state.active_head_id,
            )
            if not isinstance(continued, dict):
                raise _error("SYNTHESIS_RESULT_INVALID", category="PRECONDITION_FAILED")
            continued_current = continued.get("current")
            if (
                not isinstance(continued_current, dict)
                or not {"version_id", "revision", "snapshot_digest"}.issubset(
                    continued_current
                )
            ):
                raise _error("SYNTHESIS_RESULT_INVALID", category="PRECONDITION_FAILED")
            continued_current = copy.deepcopy(continued_current)
            continued_current.setdefault("project_id", project.name)
            result = copy.deepcopy(continued)
            result.update(
                {
                    "result": "RESUMED",
                    "current": continued_current,
                    "revision": continued_current["revision"],
                    **dashboard_fields,
                }
            )
            result.setdefault(
                "write_mode",
                "VERSION_CONTEXT"
                if continued_current["revision"] != state.revision
                else "NONE",
            )
            release = _continue_v2_release(project)
            if release is not None:
                result.update(release)
            return result
        if _source_mapping_complete(project, snapshot):
            try:
                bridged = local_pdf_parse.register_pdf_only_evidence_for_approved_parse(
                    project,
                    session_id=session_id,
                    expected_revision=state.revision,
                    expected_head_id=state.active_head_id,
                )
            except local_pdf_parse.LocalPdfParseError as exc:
                if exc.code not in _PDF_EVIDENCE_BRIDGE_HOLD_CODES:
                    raise
            else:
                if not isinstance(bridged, dict) or not isinstance(
                    bridged.get("current"), dict
                ):
                    raise _error("PAPER_EVIDENCE_RESULT_INVALID", category="PRECONDITION_FAILED")
                bridge_current = bridged["current"]
                if not {"version_id", "revision", "snapshot_digest"}.issubset(
                    bridge_current
                ):
                    raise _error("PAPER_EVIDENCE_RESULT_INVALID", category="PRECONDITION_FAILED")
                result = copy.deepcopy(bridged)
                result.update(
                    {
                        "result": "RESUMED",
                        "current": copy.deepcopy(bridge_current),
                        "revision": bridge_current["revision"],
                        **dashboard_fields,
                    }
                )
                return result
    if dashboard_failure is not None:
        # A dashboard restart is only an access-plane failure.  Preserve an
        # already-published parse/evidence human gate so resume remains
        # state-driven and does not hide the actionable product reason behind
        # a transient dashboard condition.  Bootstrap/source-role states do
        # not have an agent_parse owner and continue to use the explicit HOLD.
        parse_owner = snapshot.get("agent_parse")
        if not (
            isinstance(parse_owner, dict)
            and parse_owner.get("status") == fresh_bootstrap.HUMAN_ACTION_REQUIRED
        ):
            return _dashboard_hold(project, current_payload, dashboard_failure)
    status, reason_code, next_action, trace = _nested_status(snapshot)
    if next_action is None:
        next_action = {"project_id": project.name, "route": "/review", "type": status}
    return {
        "result": "RESUMED",
        "status": status,
        "reason_code": reason_code,
        "current": current_payload,
        "revision": current_payload["revision"],
        "write_mode": "NONE",
        "next_action": copy.deepcopy(next_action),
        "trace": copy.deepcopy(trace) if trace is not None else {"event_count": 0},
        **dashboard_fields,
    }


def _fresh(
    topic: str,
    project: Path,
    authorized_pdf_folder: Path,
    *,
    rq: str | None = None,
    scope: str | None = None,
    output_format: str | None = None,
) -> dict[str, Any]:
    try:
        bootstrap_kwargs: dict[str, Any] = {
            "topic": topic,
            "authorized_pdf_folder": authorized_pdf_folder,
        }
        if rq is not None:
            bootstrap_kwargs["rq"] = rq
        if scope is not None:
            bootstrap_kwargs["scope"] = scope
        if output_format is not None:
            bootstrap_kwargs["output_format"] = output_format
        result = FreshAgentBootstrap(project).start(
            **bootstrap_kwargs,
        )
    except FreshAgentBootstrapError as exc:
        input_codes = {
            "TOPIC_INVALID",
            "AUTHORIZED_PDF_INVALID",
            "AUTHORIZED_PDF_FOLDER_INVALID",
            "AUTHORIZED_PDF_COUNT_INVALID",
            "AUTHORIZED_PDF_DUPLICATE_HASH",
        }
        category = "INVALID_INPUT" if exc.code in input_codes else "PRECONDITION_FAILED"
        write_mode = "zero_write" if exc.write_mode == "NONE" else exc.write_mode.casefold()
        raise _error(exc.code, category=category, write_mode=write_mode) from exc
    if not isinstance(result, dict) or not isinstance(result.get("current"), dict):
        raise _error("FRESH_RESULT_INVALID", category="PRECONDITION_FAILED")
    if result.get("project_id") != project.name:
        raise _error("FRESH_RESULT_INVALID", category="PRECONDITION_FAILED")
    current = copy.deepcopy(result["current"])
    if not {"version_id", "revision", "snapshot_digest"}.issubset(current):
        raise _error("FRESH_RESULT_INVALID", category="PRECONDITION_FAILED")
    if "project_id" in current and current["project_id"] != project.name:
        raise _error("FRESH_RESULT_INVALID", category="PRECONDITION_FAILED")
    current.setdefault("project_id", project.name)
    return {
        **copy.deepcopy(result),
        "result": "FRESH",
        "current": current,
        "write_mode": "VERSION_CONTEXT",
        "revision": current["revision"],
    }


def start_or_resume_review(
    topic: str,
    explicit_project_root: str | Path,
    authorized_pdf_folder: str | Path,
    rq: str | None = None,
    scope: str | None = None,
    output_format: str | None = None,
) -> dict[str, Any]:
    """Start a fresh review or resume its explicit project root."""
    topic = _required_text(topic, "TOPIC_INVALID")
    rq = _optional_text(rq, "REVIEW_QUESTION_INVALID")
    scope = _optional_text(scope, "SCOPE_INVALID")
    output_format = _optional_text(output_format, "OUTPUT_FORMAT_INVALID")
    project, is_fresh = _explicit_root(explicit_project_root)
    try:
        authorized = Path(authorized_pdf_folder)
    except (TypeError, ValueError) as exc:
        raise _error("AUTHORIZED_PDF_FOLDER_INVALID") from exc
    try:
        authorized_pdfs = fresh_bootstrap._authorized_pdfs(authorized)
    except fresh_bootstrap.FreshAgentBootstrapError as exc:
        raise _error(exc.code) from exc
    if is_fresh:
        return _fresh(
            topic,
            project,
            authorized,
            rq=rq,
            scope=scope,
            output_format=output_format,
        )
    return _resume(project, authorized_pdfs)


__all__ = ["PublicReviewEntryError", "start_or_resume_review"]
