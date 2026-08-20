"""Agent-facing bootstrap for a fresh, source-bound review project.

This adapter deliberately stops at the first scientific decision: assigning a
MAIN/SI role to an authorized PDF.  It creates only the existing project,
VersionContext and source-archive seams, then starts the existing Dashboard
for the human action.  It neither creates a second authority nor treats the
Dashboard as a source producer.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from review_writer.product_foundation import ProductFoundationError, VersionContext
from review_writer.project.paper_evidence_store import (
    PaperEvidenceStoreError,
    project_write_lock,
)
from review_writer.project.source_truth import canonical_digest
from review_writer.project.vertical_review import (
    VerticalReviewError,
    confirm_review_brief,
    initialize_review,
)
from view.serve_review_dashboard import (
    SOURCE_ARCHIVE_RELATIVE,
    SourceArchivePreflightError,
    _source_archive_preflight,
)


HUMAN_ACTION_REQUIRED = "HUMAN_ACTION_REQUIRED"
SOURCE_ROLE_HUMAN_ACTION_REQUIRED = "SOURCE_ROLE_HUMAN_ACTION_REQUIRED"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_OWNED_DASHBOARDS: dict[int, subprocess.Popen[bytes]] = {}
_DASHBOARD_START_TIMEOUT_SECONDS = 2.5
_DASHBOARD_POLL_SECONDS = 0.05
_FRESH_PROJECT_FILES = frozenset(
    {
        ".paper_evidence.lock",
        "00_brief/review_state.json",
        "01_evidence/evidence_cards.jsonl",
        "01_evidence/exception_queue.json",
        "02_claims/claim_projection.jsonl",
        "03_review/risk_decisions.json",
        "00_sources/manual_upload/inbox/source_bundle.zip",
        ".review-writer/version_context/current.json",
    }
)
_FRESH_PROJECT_DIRECTORIES = frozenset(
    {
        "00_brief",
        "01_evidence",
        "02_claims",
        "03_review",
        "00_sources",
        "00_sources/manual_upload",
        "00_sources/manual_upload/inbox",
        ".review-writer",
        ".review-writer/version_context",
        ".review-writer/version_context/versions",
        ".review-writer/version_context/branches",
    }
)


class FreshAgentBootstrapError(ValueError):
    """Stable fail-closed error for fresh Agent bootstrap input or runtime."""

    def __init__(
        self,
        code: str,
        *,
        write_mode: str = "NONE",
        runtime_diagnostic: str | None = None,
    ) -> None:
        self.code = code
        self.write_mode = write_mode
        self.runtime_diagnostic = runtime_diagnostic
        super().__init__(code)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{os.urandom(12).hex()}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fresh_project_root(value: str | Path) -> Path:
    try:
        supplied = Path(value)
    except (TypeError, ValueError) as exc:
        raise FreshAgentBootstrapError("PROJECT_ROOT_INVALID") from exc
    if not supplied.is_absolute():
        raise FreshAgentBootstrapError("PROJECT_ROOT_INVALID")
    if supplied.is_symlink():
        raise FreshAgentBootstrapError("PROJECT_ROOT_INVALID")
    try:
        parent = supplied.parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise FreshAgentBootstrapError("PROJECT_ROOT_INVALID") from exc
    if not parent.is_dir():
        raise FreshAgentBootstrapError("PROJECT_ROOT_INVALID")
    if supplied.exists() and (
        not supplied.is_dir() or any(supplied.iterdir())
    ):
        raise FreshAgentBootstrapError("PROJECT_ROOT_NOT_EMPTY")
    return parent / supplied.name


def _authorized_pdf(value: str | Path) -> Path:
    try:
        supplied = Path(value)
    except (TypeError, ValueError) as exc:
        raise FreshAgentBootstrapError("AUTHORIZED_PDF_FOLDER_INVALID") from exc
    if not supplied.is_absolute() or supplied.is_symlink():
        raise FreshAgentBootstrapError("AUTHORIZED_PDF_FOLDER_INVALID")
    try:
        folder = supplied.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise FreshAgentBootstrapError("AUTHORIZED_PDF_FOLDER_INVALID") from exc
    if not folder.is_dir():
        raise FreshAgentBootstrapError("AUTHORIZED_PDF_FOLDER_INVALID")
    candidates = sorted(
        (
            path
            for path in folder.iterdir()
            if not path.is_symlink()
            and path.is_file()
            and path.suffix.casefold() == ".pdf"
        ),
        key=lambda path: path.name.casefold(),
    )
    if not 1 <= len(candidates) <= 3:
        raise FreshAgentBootstrapError("AUTHORIZED_PDF_COUNT_INVALID")
    selected = candidates[0]
    try:
        if selected.stat().st_size <= 0 or selected.read_bytes()[:5] != b"%PDF-":
            raise FreshAgentBootstrapError("AUTHORIZED_PDF_INVALID")
    except OSError as exc:
        raise FreshAgentBootstrapError("AUTHORIZED_PDF_INVALID") from exc
    return selected


def _build_authorized_archive(pdf: Path, destination_parent: Path) -> tuple[Path, str]:
    temporary_file = tempfile.NamedTemporaryFile(
        dir=destination_parent,
        prefix=".fresh-agent-source-",
        suffix=".zip",
        delete=False,
    )
    archive_path = Path(temporary_file.name)
    temporary_file.close()
    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(pdf, arcname=pdf.name)
        return archive_path, _sha256(pdf)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        archive_path.unlink(missing_ok=True)
        raise FreshAgentBootstrapError("SOURCE_ARCHIVE_BUILD_FAILED") from exc


def _preflight_source_archive(archive: Path, expected_digest: str) -> dict[str, Any]:
    try:
        preflight = _source_archive_preflight(archive)
    except SourceArchivePreflightError as exc:
        raise FreshAgentBootstrapError(exc.code) from exc
    member = preflight.get("member") if isinstance(preflight, dict) else None
    if not isinstance(member, dict) or member.get("sha256") != expected_digest:
        raise FreshAgentBootstrapError("AUTHORIZED_PDF_STALE")
    return preflight


def _publish_source_archive(
    project: Path,
    archive: Path,
    expected_digest: str,
    *,
    preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preflight = preflight or _preflight_source_archive(archive, expected_digest)

    destination = project / SOURCE_ARCHIVE_RELATIVE
    if destination.exists() or destination.is_symlink():
        raise FreshAgentBootstrapError("SOURCE_ARCHIVE_EXISTS")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as target:
            temporary = Path(target.name)
            with archive.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        published = _source_archive_preflight(temporary)
        if published.get("archive_sha256") != preflight.get("archive_sha256"):
            raise FreshAgentBootstrapError("SOURCE_ARCHIVE_STALE")
        os.replace(temporary, destination)
        temporary = None
    except FreshAgentBootstrapError:
        raise
    except (OSError, SourceArchivePreflightError) as exc:
        raise FreshAgentBootstrapError("SOURCE_ARCHIVE_WRITE_FAILED") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return preflight


def _open_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _dashboard_runtime_diagnostic(process: subprocess.Popen[bytes], log_path: Path) -> str:
    if process.poll() is None:
        return "CHILD_HEALTH_TIMEOUT"
    try:
        text = log_path.read_bytes()[:16_384].decode("utf-8", errors="replace").casefold()
    except OSError:
        return "CHILD_EARLY_EXIT"
    if "address already in use" in text:
        return "PYTHON_PORT_ERROR"
    if "permissionerror" in text or "permission denied" in text:
        return "PYTHON_PERMISSION_ERROR"
    if "modulenotfounderror" in text or "importerror" in text:
        return "PYTHON_IMPORT_ERROR"
    return "CHILD_EARLY_EXIT"


def _terminate_dashboard_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _start_dashboard(review_root: Path) -> tuple[str, int]:
    port = _open_port()
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir="/tmp",
        prefix="review-writer-dashboard-",
        suffix=".log",
        delete=False,
    ) as diagnostic_log:
        diagnostic_path = Path(diagnostic_log.name)
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(_REPO_ROOT / "view" / "serve_review_dashboard.py"),
                    "--review-root",
                    str(review_root),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=_REPO_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=diagnostic_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except PermissionError as exc:
            raise FreshAgentBootstrapError(
                "DASHBOARD_START_FAILED",
                runtime_diagnostic="PYTHON_PERMISSION_ERROR",
            ) from exc
        except OSError as exc:
            raise FreshAgentBootstrapError(
                "DASHBOARD_START_FAILED",
                runtime_diagnostic="CHILD_SPAWN_ERROR",
            ) from exc
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + _DASHBOARD_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise FreshAgentBootstrapError(
                    "DASHBOARD_START_FAILED",
                    runtime_diagnostic=_dashboard_runtime_diagnostic(
                        process, diagnostic_path
                    ),
                )
            try:
                with urlopen(f"{base_url}/api/projects", timeout=0.2) as response:
                    if response.status == 200:
                        _OWNED_DASHBOARDS[process.pid] = process
                        return base_url, process.pid
            except (OSError, URLError):
                time.sleep(_DASHBOARD_POLL_SECONDS)
        if process.poll() is not None:
            raise FreshAgentBootstrapError(
                "DASHBOARD_START_FAILED",
                runtime_diagnostic=_dashboard_runtime_diagnostic(process, diagnostic_path),
            )
        raise FreshAgentBootstrapError(
            "DASHBOARD_START_FAILED",
            runtime_diagnostic="CHILD_HEALTH_TIMEOUT",
        )
    finally:
        if process.pid not in _OWNED_DASHBOARDS and process.poll() is None:
            _terminate_dashboard_process(process)


def _stop_dashboard(pid: int) -> None:
    process = _OWNED_DASHBOARDS.pop(pid, None)
    if process is None:
        raise FreshAgentBootstrapError("DASHBOARD_NOT_OWNED")
    try:
        _terminate_dashboard_process(process)
    except subprocess.TimeoutExpired as exc:
        raise FreshAgentBootstrapError("DASHBOARD_STOP_FAILED") from exc


def _fresh_project_relative_allowed(relative: Path) -> bool:
    text = relative.as_posix()
    if text in _FRESH_PROJECT_FILES or text in _FRESH_PROJECT_DIRECTORIES:
        return True
    parts = relative.parts
    return (
        len(parts) == 4
        and parts[:3] == (".review-writer", "version_context", "versions")
        and Path(parts[3]).suffix == ".json"
    ) or (
        len(parts) == 4
        and parts[:3] == (".review-writer", "version_context", "branches")
        and Path(parts[3]).suffix == ".json"
    )


def _rollback_fresh_project(project: Path, *, existed_before: bool) -> bool:
    """Remove only the exact fresh-project objects created by this adapter."""
    if not project.exists():
        return existed_before is False
    if project.is_symlink() or not project.is_dir():
        return False
    try:
        entries = sorted(project.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    except OSError:
        return False
    for path in entries:
        try:
            relative = path.relative_to(project)
            if not _fresh_project_relative_allowed(relative) or path.is_symlink():
                return False
        except (OSError, ValueError):
            return False
    try:
        for path in entries:
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        if existed_before:
            return project.is_dir() and not any(project.iterdir())
        project.rmdir()
        return not project.exists()
    except OSError:
        return False


class FreshAgentBootstrap:
    """Create one fresh project and stop at the first human source-role gate."""

    def __init__(self, explicit_project_root: str | Path) -> None:
        self.project_root = _fresh_project_root(explicit_project_root)

    @staticmethod
    def stop_owned_dashboard(pid: int) -> None:
        _stop_dashboard(pid)

    def start(
        self,
        *,
        topic: str,
        authorized_pdf_folder: str | Path,
    ) -> dict[str, Any]:
        if not isinstance(topic, str) or not topic.strip():
            raise FreshAgentBootstrapError("TOPIC_INVALID")
        normalized_topic = topic.strip()
        selected_pdf = _authorized_pdf(authorized_pdf_folder)
        staged_archive, selected_digest = _build_authorized_archive(
            selected_pdf,
            self.project_root.parent,
        )
        existed_before = self.project_root.exists()
        dashboard_pid: int | None = None
        project_touched = False
        try:
            preflight = _preflight_source_archive(staged_archive, selected_digest)
            # The Dashboard is an owned runtime prerequisite.  Start and
            # health-check it while the fresh project root is still empty so
            # a startup failure cannot publish partial canonical bytes.
            dashboard_url, dashboard_pid = _start_dashboard(self.project_root.parent)
            project_touched = True
            try:
                project = initialize_review(
                    self.project_root.parent,
                    self.project_root.name,
                    {
                        "topic": normalized_topic,
                        "review_question": (
                            "What source-bound evidence is available for the supplied topic?"
                        ),
                    },
                )
                confirm_review_brief(project)
            except (OSError, VerticalReviewError) as exc:
                raise FreshAgentBootstrapError("PROJECT_BOOTSTRAP_FAILED") from exc

            preflight = _publish_source_archive(
                project,
                staged_archive,
                selected_digest,
                preflight=preflight,
            )
            try:
                with project_write_lock(project):
                    context = VersionContext.load(project)
                    state = context.state()
                    current = context.view_version(state.current_version_id)
                    next_action = {
                        "project_id": project.name,
                        "route": "/review",
                        "type": HUMAN_ACTION_REQUIRED,
                        "reason_code": SOURCE_ROLE_HUMAN_ACTION_REQUIRED,
                    }
                    trace = [
                        {
                            "tool": "source_archive_preflight",
                            "status": "SUCCESS",
                            "result_digest": canonical_digest(preflight),
                        },
                        {
                            "tool": "start_dashboard",
                            "status": "SUCCESS",
                            "dashboard_url": dashboard_url,
                        },
                        {"tool": "initialize_review", "status": "SUCCESS"},
                        {"tool": "confirm_review_brief", "status": "SUCCESS"},
                    ]
                    bootstrap = {
                        "run_id": _new_id("fresh-bootstrap-run"),
                        "actor_type": "generator_agent",
                        "status": HUMAN_ACTION_REQUIRED,
                        "reason_code": SOURCE_ROLE_HUMAN_ACTION_REQUIRED,
                        "topic_digest": canonical_digest({"topic": normalized_topic}),
                        "source_archive": copy.deepcopy(preflight),
                        "tool_trace": trace,
                        "next_action": next_action,
                    }
                    node = context.publish_active_head(
                        {**copy.deepcopy(dict(current.snapshot)), "agent_bootstrap": bootstrap},
                        expected_head_id=state.active_head_id,
                        expected_revision=state.revision,
                        version_id=_new_id("agent-bootstrap"),
                    )
            except (PaperEvidenceStoreError, ProductFoundationError, OSError) as exc:
                raise FreshAgentBootstrapError("BOOTSTRAP_VERSION_CONTEXT_FAILED") from exc

            return {
                "status": HUMAN_ACTION_REQUIRED,
                "reason_code": SOURCE_ROLE_HUMAN_ACTION_REQUIRED,
                "project_id": project.name,
                "project_root": str(project),
                "dashboard_url": dashboard_url,
                "dashboard_pid": dashboard_pid,
                "next_action": next_action,
                "current": {
                    "version_id": node.version_id,
                    "revision": context.state().revision,
                    "snapshot_digest": node.snapshot_digest,
                },
                "trace": {"run_id": bootstrap["run_id"], "event_count": len(trace)},
            }
        except Exception as exc:
            stop_error: FreshAgentBootstrapError | None = None
            if dashboard_pid is not None:
                try:
                    _stop_dashboard(dashboard_pid)
                except FreshAgentBootstrapError as stop_exc:
                    stop_error = stop_exc
            if project_touched and not _rollback_fresh_project(
                self.project_root,
                existed_before=existed_before,
            ):
                raise FreshAgentBootstrapError(
                    "BOOTSTRAP_ROLLBACK_FAILED",
                    write_mode="PARTIAL",
                ) from exc
            if stop_error is not None:
                raise stop_error from exc
            if isinstance(exc, FreshAgentBootstrapError):
                exc.write_mode = "NONE"
                raise
            raise FreshAgentBootstrapError("BOOTSTRAP_FAILED") from exc
        finally:
            staged_archive.unlink(missing_ok=True)
