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
    current = SimpleNamespace(snapshot=snapshot)
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
