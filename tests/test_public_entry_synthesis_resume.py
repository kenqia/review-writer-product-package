"""Focused tests for the public parse-to-synthesis resume seam."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from review_writer.agent import public_entry


class _Context:
    def __init__(self, state: object, current: object) -> None:
        self._state = state
        self._current = current

    def state(self) -> object:
        return self._state

    def view_version(self, _version_id: str) -> object:
        return self._current


def _patch_resume_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    evidence: dict[str, object],
    continuation: object,
) -> tuple[Path, SimpleNamespace, dict[str, object], list[dict[str, object]]]:
    project = tmp_path / "resume-project"
    (project / "01_evidence/mineru").mkdir(parents=True)
    state = SimpleNamespace(
        project_id=project.name,
        current_version_id="old-version",
        revision=7,
        active_head_id="old-version",
    )
    snapshot: dict[str, object] = {
        "currentness": "current",
        "agent_parse": {
            "status": "HUMAN_ACTION_REQUIRED",
            "reason_code": "PAPER_EVIDENCE_HUMAN_ACTION_REQUIRED",
            "session_id": "parse-session-1",
            "next_action": {
                "project_id": project.name,
                "route": "/review",
                "type": "HUMAN_ACTION_REQUIRED",
            },
        },
    }
    current = SimpleNamespace(
        snapshot=snapshot,
        is_current=True,
        is_active_head=True,
        can_write=True,
        version_id=state.current_version_id,
        snapshot_digest="a" * 64,
    )
    monkeypatch.setattr(
        public_entry.VersionContext,
        "load",
        lambda _project: _Context(state, current),
    )
    monkeypatch.setattr(public_entry, "_validate_resume_source", lambda *args: None)
    monkeypatch.setattr(
        public_entry,
        "_current_payload",
        lambda _project, _snapshot: {
            "project_id": project.name,
            "version_id": "old-version",
            "revision": state.revision,
            "snapshot_digest": "a" * 64,
        },
    )
    monkeypatch.setattr(
        public_entry,
        "paper_evidence_state",
        lambda _project: evidence,
        raising=False,
    )
    calls: list[dict[str, object]] = []

    class FakeGeneratorSession:
        def __init__(self, explicit_project: Path) -> None:
            calls.append({"project": explicit_project})

        def continue_session(
            self,
            session_id: str,
            *,
            expected_revision: int,
            expected_head_id: str,
        ) -> object:
            calls.append(
                {
                    "session_id": session_id,
                    "expected_revision": expected_revision,
                    "expected_head_id": expected_head_id,
                }
            )
            if isinstance(continuation, BaseException):
                raise continuation
            return continuation

    monkeypatch.setattr(
        public_entry, "GeneratorSession", FakeGeneratorSession, raising=False
    )
    return project, state, snapshot, calls


def test_resume_continues_existing_parse_session_after_approved_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    continuation = {
        "status": "HUMAN_ACTION_REQUIRED",
        "write_mode": "VERSION_CONTEXT",
        "session_id": "parse-session-1",
        "current": {
            "project_id": "resume-project",
            "version_id": "synthesis-version",
            "revision": 8,
            "snapshot_digest": "b" * 64,
        },
        "next_action": {
            "project_id": "resume-project",
            "route": "/review",
            "type": "HUMAN_ACTION_REQUIRED",
        },
    }
    project, state, _snapshot, calls = _patch_resume_project(
        monkeypatch,
        tmp_path,
        evidence={"workflow_can_continue": True},
        continuation=continuation,
    )

    result = public_entry._resume(project, ())

    assert calls == [
        {"project": project},
        {
            "session_id": "parse-session-1",
            "expected_revision": state.revision,
            "expected_head_id": state.active_head_id,
        },
    ]
    assert result["result"] == "RESUMED"
    assert result["write_mode"] == "VERSION_CONTEXT"
    assert result["current"]["version_id"] == "synthesis-version"
    assert result["revision"] == 8


def test_resume_continuation_survives_dashboard_restart_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    continuation = {
        "status": "HUMAN_ACTION_REQUIRED",
        "write_mode": "VERSION_CONTEXT",
        "session_id": "parse-session-1",
        "current": {
            "project_id": "resume-project",
            "version_id": "synthesis-version",
            "revision": 8,
            "snapshot_digest": "b" * 64,
        },
        "next_action": {
            "project_id": "resume-project",
            "route": "/review",
            "type": "HUMAN_ACTION_REQUIRED",
        },
    }
    project, state, _snapshot, calls = _patch_resume_project(
        monkeypatch,
        tmp_path,
        evidence={"workflow_can_continue": True},
        continuation=continuation,
    )

    def fail_start(_review_root: Path) -> tuple[str, int]:
        raise public_entry.fresh_bootstrap.FreshAgentBootstrapError(
            "DASHBOARD_START_FAILED", runtime_diagnostic="CHILD_EARLY_EXIT"
        )

    monkeypatch.setattr(public_entry.fresh_bootstrap, "_start_dashboard", fail_start)

    result = public_entry._resume(project, ())

    assert calls == [
        {"project": project},
        {
            "session_id": "parse-session-1",
            "expected_revision": state.revision,
            "expected_head_id": state.active_head_id,
        },
    ]
    assert result["result"] == "RESUMED"
    assert result["status"] == "HUMAN_ACTION_REQUIRED"
    assert result["dashboard_url"] is None
    assert result["current"]["version_id"] == "synthesis-version"
    assert result["revision"] == 8


@pytest.mark.parametrize(
    "evidence",
    [{}, {"workflow_can_continue": False}],
    ids=["missing", "not-approved"],
)
def test_resume_keeps_old_state_without_continuing_unapproved_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence: dict[str, object],
) -> None:
    project, state, _snapshot, calls = _patch_resume_project(
        monkeypatch,
        tmp_path,
        evidence=evidence,
        continuation={"status": "unexpected"},
    )

    result = public_entry._resume(project, ())

    assert calls == []
    assert result["status"] == "HUMAN_ACTION_REQUIRED"
    assert result["reason_code"] == "PAPER_EVIDENCE_HUMAN_ACTION_REQUIRED"
    assert result["write_mode"] == "NONE"
    assert result["current"]["version_id"] == "old-version"
    assert result["revision"] == state.revision


def test_resume_does_not_swallow_generator_continuation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = RuntimeError("continuation failed")
    project, _state, _snapshot, _calls = _patch_resume_project(
        monkeypatch,
        tmp_path,
        evidence={"workflow_can_continue": True},
        continuation=failure,
    )

    with pytest.raises(RuntimeError, match="continuation failed"):
        public_entry._resume(project, ())


def test_resume_publishes_same_version_v2_release_after_human_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    continuation = {
        "status": "HUMAN_ACTION_REQUIRED",
        "write_mode": "NONE",
        "session_id": "parse-session-1",
        "candidate": {"version": "v2"},
        "current": {
            "project_id": "resume-project",
            "version_id": "dashboard-v2-approval",
            "revision": 9,
            "snapshot_digest": "c" * 64,
        },
    }
    project, state, snapshot, calls = _patch_resume_project(
        monkeypatch,
        tmp_path,
        evidence={"workflow_can_continue": True},
        continuation=continuation,
    )
    snapshot["generator_runtime"] = {
        "schema_version": "review-writer.generator-runtime.v1",
        "phase": "v2",
        "candidate": {"version": "v2"},
        "human_decision": {"actor_type": "human_researcher"},
    }
    monkeypatch.setattr(
        public_entry,
        "manuscript_state",
        lambda _project: {
            "status": "approved",
            "workflow_can_continue": True,
            "reason_code": "MANUSCRIPT_APPROVED",
        },
        raising=False,
    )
    release_calls: list[Path] = []
    markdown = project / "05_release/self_reviewed_draft.md"
    docx = project / "05_release/self_reviewed_draft.docx"

    def fake_build(explicit_project: Path) -> dict[str, object]:
        release_calls.append(explicit_project)
        return {
            "status": "SELF_REVIEWED_DRAFT",
            "release_level": "SELF_REVIEWED_DRAFT",
            "snapshot": markdown,
            "docx": docx,
        }

    monkeypatch.setattr(public_entry, "build_project_release", fake_build, raising=False)

    result = public_entry._resume(project, ())

    assert calls[-1] == {
        "session_id": "parse-session-1",
        "expected_revision": state.revision,
        "expected_head_id": state.active_head_id,
    }
    assert release_calls == [project]
    assert result["release"]["status"] == "SELF_REVIEWED_DRAFT"
    assert result["release"]["markdown_path"] == "05_release/self_reviewed_draft.md"
    assert result["release"]["docx_path"] == "05_release/self_reviewed_draft.docx"
    assert result["release"]["version_context"] == {
        "version_id": "old-version",
        "revision": state.revision,
        "snapshot_digest": "a" * 64,
    }


def _prepare_v2_release_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    human_actor: str = "human_researcher",
) -> tuple[Path, SimpleNamespace]:
    project, state, snapshot, _calls = _patch_resume_project(
        monkeypatch,
        tmp_path,
        evidence={"workflow_can_continue": True},
        continuation={"status": "HUMAN_ACTION_REQUIRED"},
    )
    snapshot["generator_runtime"] = {
        "schema_version": "review-writer.generator-runtime.v1",
        "phase": "v2",
        "candidate": {"version": "v2"},
        "human_decision": {"actor_type": human_actor},
    }
    monkeypatch.setattr(
        public_entry,
        "manuscript_state",
        lambda _project: {"workflow_can_continue": True},
        raising=False,
    )
    return project, state


def test_unapproved_v2_release_is_zero_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _state = _prepare_v2_release_gate(
        monkeypatch, tmp_path, human_actor="simulated_researcher_agent"
    )
    before = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    monkeypatch.setattr(
        public_entry,
        "build_project_release",
        lambda _project: pytest.fail("release must not build without human approval"),
        raising=False,
    )

    assert public_entry._continue_v2_release(project) is None
    after = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    assert after == before


def test_current_release_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _state = _prepare_v2_release_gate(monkeypatch, tmp_path)
    docx = project / "05_release/self_reviewed_draft.docx"
    docx.parent.mkdir(parents=True)
    docx.write_bytes(b"current-release")
    monkeypatch.setattr(
        public_entry,
        "_release_artifact_state",
        lambda _project: ("current", "SELF_REVIEWED_DRAFT", docx),
    )
    monkeypatch.setattr(
        public_entry,
        "build_project_release",
        lambda _project: pytest.fail("current release must not rebuild"),
        raising=False,
    )

    result = public_entry._continue_v2_release(project)

    assert result == {
        "release_status": "SELF_REVIEWED_DRAFT",
        "release": {
            "status": "SELF_REVIEWED_DRAFT",
            "release_level": "SELF_REVIEWED_DRAFT",
            "markdown_path": "05_release/self_reviewed_draft.md",
            "docx_path": "05_release/self_reviewed_draft.docx",
            "version_context": {
                "version_id": "old-version",
                "revision": 7,
                "snapshot_digest": "a" * 64,
            },
        },
    }


def test_stale_release_exposes_final_regenerate_next_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _state = _prepare_v2_release_gate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        public_entry,
        "_release_artifact_state",
        lambda _project: ("stale", None, None),
    )
    monkeypatch.setattr(
        public_entry,
        "build_project_release",
        lambda _project: pytest.fail("stale release must not rebuild"),
        raising=False,
    )

    assert public_entry._continue_v2_release(project) == {
        "release_status": "RELEASE_OUTDATED",
        "next_action": {
            "project_id": project.name,
            "route": "/final",
            "type": "REGENERATE_RELEASE",
            "reason_code": "RELEASE_OUTDATED",
        },
    }
