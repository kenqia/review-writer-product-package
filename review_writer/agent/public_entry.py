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
from urllib.parse import urlparse

from review_writer.product_foundation import ProductFoundationError, VersionContext
from review_writer.product_foundation.project_root import resolve_project_root
from view.serve_review_dashboard import PublicProjectResumeError, _resume_artifact_refs

from . import fresh_bootstrap

# Keep the public adapter's seam explicit while reusing the existing fresh
# bootstrap implementation.  Tests and callers can patch/inspect this seam
# without introducing a second bootstrap implementation.
FreshAgentBootstrap = fresh_bootstrap.FreshAgentBootstrap
FreshAgentBootstrapError = fresh_bootstrap.FreshAgentBootstrapError


_SAFE_TEXT_MAX = 20_000
_SAFE_URL_HOSTS = {"127.0.0.1", "localhost", "::1"}


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
    parsed = urlparse(value)
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


def _nested_status(
    snapshot: dict[str, Any],
) -> tuple[str, str | None, dict[str, Any] | None, Any]:
    for owner_key in ("agent_bootstrap", "generator_runtime", "agent_parse"):
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
    status, reason_code, next_action, trace = _nested_status(snapshot)
    if next_action is None:
        next_action = {"project_id": project.name, "route": "/review", "type": status}
    return {
        "result": "RESUMED",
        "status": status,
        "reason_code": reason_code,
        "dashboard_url": _dashboard_url(snapshot),
        "current": current_payload,
        "revision": current_payload["revision"],
        "write_mode": "NONE",
        "next_action": copy.deepcopy(next_action),
        "trace": copy.deepcopy(trace) if trace is not None else {"event_count": 0},
    }


def _fresh(topic: str, project: Path, authorized_pdf_folder: Path) -> dict[str, Any]:
    try:
        result = FreshAgentBootstrap(project).start(
            topic=topic,
            authorized_pdf_folder=authorized_pdf_folder,
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
    _optional_text(rq, "REVIEW_QUESTION_INVALID")
    _optional_text(scope, "SCOPE_INVALID")
    _optional_text(output_format, "OUTPUT_FORMAT_INVALID")
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
        return _fresh(topic, project, authorized)
    return _resume(project, authorized_pdfs)


__all__ = ["PublicReviewEntryError", "start_or_resume_review"]
