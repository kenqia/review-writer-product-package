"""FR-027 public Agent smoke; full release remains an explicit HOLD."""

from __future__ import annotations

import ast
import hashlib
import http.client
import inspect
import json
import os
import textwrap
from pathlib import Path
from urllib.parse import urlsplit

from review_writer.agent import fresh_bootstrap
from tests.test_fresh_bootstrap_source_set import (
    _run_child_json as _run_agent_child_json,
    _write_pdf,
)
from tests.support.agent_e2e_harness import run_agent


TOPIC = "A bounded N=3 Agent-first source-set review"


def _request(base: str, method: str, route: str, payload: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
    parsed = urlsplit(base)
    body = b"" if payload is None else json.dumps(payload).encode()
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    try:
        connection.request(method, route, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        decoded = json.loads(response.read().decode())
        assert isinstance(decoded, dict)
        return response.status, decoded
    finally:
        connection.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifacts(root: Path) -> dict[str, object]:
    paths = {"markdown": "05_release/self_reviewed_draft.md", "docx": "05_release/self_reviewed_draft.docx", "release_snapshot": "05_release/release_snapshot.json"}
    return {name: {"path": relative, "exists": (path := root / relative).is_file(), "sha256": _digest(path) if path.is_file() else None} for name, relative in paths.items()}


def test_agent_wrapper_ast_has_no_internal_bypass() -> None:
    source = textwrap.dedent(inspect.getsource(run_agent))
    tree = ast.parse(source)
    forbidden = {"subprocess", "curl", "pytest", "VersionContext", "local_pdf_parse", "generator_runtime", "serve_review_dashboard", "public_entry"}
    assert not any(isinstance(node, ast.Name) and node.id in forbidden for node in ast.walk(tree))
    assert "/api/" not in source
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert len(calls) == 1
    call = calls[0]
    assert isinstance(call.func, ast.Name) and call.func.id == "start_or_resume_review"


def test_agent_first_n3_smoke_stops_at_parse_quality_gate(tmp_path: Path) -> None:
    """Real public fresh→mapping→resume smoke; receipt is deliberately HOLD."""
    folder = tmp_path / "authorized-pdfs"
    folder.mkdir()
    for name, payload in (("a.pdf", b"A"), ("b.pdf", b"B"), ("c.pdf", b"C")):
        _write_pdf(folder, name, payload)
    root = tmp_path / "projects" / "agent-first-n3"
    root.parent.mkdir()
    receipt_path = tmp_path / "agent-e2e-receipt.json"
    receipt: dict[str, object] = {
        "schema_version": "agent-e2e-receipt.v1", "result": "AGENT_E2E_HOLD",
        "model": os.environ.get("REVIEW_WRITER_AGENT_MODEL", "not-run"),
        "provider": os.environ.get("REVIEW_WRITER_AGENT_PROVIDER", "local-test-wrapper"),
        "version": os.environ.get("REVIEW_WRITER_AGENT_VERSION", "fr027-harness-v1"),
        "agent_mode": "public_entry_wrapper_only; no model runtime invoked", "initial_prompt": TOPIC,
        "tool_skill_call_sequence": [], "human_gates": [], "researcher_operations": [],
        "project_root": str(root), "input_pdf_hashes": {p.name: _digest(p) for p in sorted(folder.glob("*.pdf"))},
        "final_version_context": None, "final_artifacts": _artifacts(root),
    }
    pid: int | None = None
    try:
        fresh = run_agent(TOPIC, root, folder)
        assert fresh["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
        assert fresh["reason_code"] == fresh_bootstrap.SOURCE_ROLE_HUMAN_ACTION_REQUIRED
        assert fresh["project_id"] == root.name
        pid = fresh.get("dashboard_pid") if isinstance(fresh.get("dashboard_pid"), int) else None
        base, project_id = fresh["dashboard_url"], str(fresh["project_id"])
        assert isinstance(base, str) and base.startswith("http://127.0.0.1:")
        status, sources = _request(base, "GET", f"/api/project/{project_id}/sources")
        assert status == 200 and isinstance(sources["preflight"], dict)
        status, history = _request(base, "GET", f"/api/project/{project_id}/history")
        assert status == 200
        members = sources["preflight"]["members"]
        assert isinstance(members, list) and len(members) == 3
        rows = [{key: member[key] for key in ("member_id", "name", "sha256", "download_id", "source_id", "study_id")} | {"document_role": "MAIN"} for member in members]
        status, mapped = _request(base, "POST", f"/api/project/{project_id}/source-mapping", {"members": rows, "archive_sha256": sources["preflight"]["archive_sha256"], "expected_revision": history["revision"]})
        assert status == 200 and mapped["status"] == "mapped"
        resumed = run_agent(TOPIC, root, folder)
        assert resumed["result"] == "RESUMED" and resumed["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
        assert resumed["reason_code"] == "PARSE_QUALITY_HUMAN_ACTION_REQUIRED" and resumed["project_id"] == fresh["project_id"]
        status, quality = _request(base, "GET", f"/api/project/{project_id}/parse-quality")
        assert status == 200 and quality["workflow_can_continue"] is False and len(quality["studies"]) == 3
        receipt.update({
            "tool_skill_call_sequence": ["public_agent.start_or_resume_review:fresh", "dashboard.sources:observer", "dashboard.history:observer", "dashboard.source-mapping:researcher", "public_agent.start_or_resume_review:resume", "dashboard.parse-quality:observer"],
            "human_gates": [{"stage": "source_mapping", "status": fresh["status"], "reason_code": fresh["reason_code"]}, {"stage": "parse_quality", "status": resumed["status"], "reason_code": resumed["reason_code"]}],
            "researcher_operations": [{"stage": "source_mapping", "actor": "human_researcher_observer", "operation": "Dashboard POST source-mapping", "member_count": 3, "status": status}],
            "final_version_context": {key: resumed["current"][key] for key in ("project_id", "version_id", "revision", "snapshot_digest")},
        })
    except Exception as exc:
        receipt.update({"result": "AGENT_E2E_FAIL", "failure": {"type": type(exc).__name__}})
        raise
    finally:
        receipt["final_artifacts"] = _artifacts(root)
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if pid is not None:
            fresh_bootstrap.FreshAgentBootstrap.stop_owned_dashboard(pid)
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["result"] == "AGENT_E2E_HOLD"


def test_agent_first_cold_process_resume_uses_same_root_and_public_wrapper(
    tmp_path: Path,
) -> None:
    """A new Agent process resumes the mapped project through the public seam."""
    folder = tmp_path / "authorized-pdfs"
    folder.mkdir()
    for name, payload in (("a.pdf", b"A"), ("b.pdf", b"B"), ("c.pdf", b"C")):
        _write_pdf(folder, name, payload)
    root = tmp_path / "projects" / "agent-cold-n3"
    root.parent.mkdir()

    child_script = """
import json
import sys
from pathlib import Path
from review_writer.agent import fresh_bootstrap
from tests.support.agent_e2e_harness import run_agent

result = run_agent(
    "A bounded N=3 Agent-first cold resume review",
    Path(sys.argv[1]),
    Path(sys.argv[2]),
)
print(json.dumps({
    "result": result.get("result"),
    "status": result.get("status"),
    "reason_code": result.get("reason_code"),
    "project_id": result.get("project_id"),
    "dashboard_url": result.get("dashboard_url"),
    "dashboard_pid": result.get("dashboard_pid"),
    "current": result.get("current"),
}))
pid = result.get("dashboard_pid")
if isinstance(pid, int):
    fresh_bootstrap.FreshAgentBootstrap.stop_owned_dashboard(pid)
"""
    first = _run_agent_child_json(child_script, root, folder)
    assert first["result"] == "FRESH"
    assert first["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
    assert first["project_id"] == root.name

    second = _run_agent_child_json(child_script, root, folder)
    assert second["result"] == "RESUMED"
    assert second["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
    assert second["reason_code"] == fresh_bootstrap.SOURCE_ROLE_HUMAN_ACTION_REQUIRED
    assert second["current"]["project_id"] == root.name

    receipt = {
        "schema_version": "agent-e2e-receipt.v1",
        "result": "AGENT_E2E_HOLD",
        "model": os.environ.get("REVIEW_WRITER_AGENT_MODEL", "not-run"),
        "provider": os.environ.get("REVIEW_WRITER_AGENT_PROVIDER", "local-test-wrapper"),
        "version": os.environ.get("REVIEW_WRITER_AGENT_VERSION", "fr027-cold-n3-v1"),
        "initial_prompt": "A bounded N=3 Agent-first cold resume review",
        "tool_skill_call_sequence": [
            "public_agent.start_or_resume_review:fresh:child-process",
            "owned_dashboard.stop:researcher-observer",
            "public_agent.start_or_resume_review:resume:child-process",
            "owned_dashboard.stop:researcher-observer",
        ],
        "human_gates": [
            {"stage": "source_mapping", "status": first["status"], "reason_code": first["reason_code"]},
            {"stage": "source_mapping", "status": second["status"], "reason_code": second["reason_code"]},
        ],
        "researcher_operations": [],
        "project_root": str(root),
        "input_pdf_hashes": {
            path.name: _digest(path) for path in sorted(folder.glob("*.pdf"))
        },
        "final_version_context": second["current"],
        "final_artifacts": {
            "markdown": {"path": "05_release/self_reviewed_draft.md", "exists": False},
            "docx": {"path": "05_release/self_reviewed_draft.docx", "exists": False},
            "release_snapshot": {"path": "05_release/release_snapshot.json", "exists": False},
        },
    }
    receipt_path = tmp_path / "agent-cold-n3-receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["result"] == "AGENT_E2E_HOLD"
