"""Source-set contract tests for the native fresh Agent bootstrap."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import subprocess
import sys
from urllib.parse import urlsplit
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from view import serve_review_dashboard as dashboard
from review_writer.agent import fresh_bootstrap, local_pdf_parse, public_entry
from review_writer.product_foundation import VersionContext
from review_writer.project import source_truth
from tests.support.agent_e2e_harness import run_agent


def _write_pdf(folder: Path, name: str, payload: bytes) -> Path:
    path = folder / name
    text = (
        payload.decode("ascii", errors="replace")
        .replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET\n".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\n"
        b"stream\n" + content + b"endstream",
    ]
    document = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(document))
        document += f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref_offset = len(document)
    document += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    document += b"0000000000 65535 f \n"
    document += b"".join(
        f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:]
    )
    document += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode("ascii")
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode("ascii")
        + b"\n%%EOF\n"
    )
    path.write_bytes(document)
    return path


def _run_child_json(script: str, *arguments: Path) -> dict[str, object]:
    repo_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(repo_root), existing_pythonpath) if item
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, *(str(argument) for argument in arguments)],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert completed.returncode == 0, (
        f"child failed with rc={completed.returncode}: "
        f"stdout={completed.stdout[-2000:]!r} stderr={completed.stderr[-2000:]!r}"
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, f"child produced no JSON: stderr={completed.stderr[-2000:]!r}"
    payload = json.loads(lines[-1])
    assert isinstance(payload, dict)
    return payload


@pytest.mark.parametrize("count", [1, 3])
def test_authorized_pdf_set_keeps_every_legal_pdf_in_deterministic_identity_order(
    tmp_path: Path, count: int
) -> None:
    folder = tmp_path / "authorized"
    folder.mkdir()
    names = ["zeta.pdf", "Alpha.PDF", "middle.pdf"][:count]
    for index, name in enumerate(names):
        _write_pdf(folder, name, f"paper-{index}".encode())

    observed = fresh_bootstrap._authorized_pdfs(folder)

    assert [path.name for path in observed] == sorted(names, key=str.casefold)
    assert [
        hashlib.sha256(path.read_bytes()).hexdigest() for path in observed
    ] == [hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in sorted(names, key=str.casefold)]


def test_authorized_archive_retains_all_members_and_stable_hash_identity(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "authorized"
    folder.mkdir()
    _write_pdf(folder, "b.pdf", b"B")
    _write_pdf(folder, "a.pdf", b"A")
    pdfs = fresh_bootstrap._authorized_pdfs(folder)

    archive_path, source_set = fresh_bootstrap._build_authorized_archive(
        pdfs, tmp_path
    )

    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["a.pdf", "b.pdf"]
        assert [archive.read(name) for name in archive.namelist()] == [
            (folder / name).read_bytes() for name in ("a.pdf", "b.pdf")
        ]
    assert [row["name"] for row in source_set] == ["a.pdf", "b.pdf"]
    assert [row["member_id"] for row in source_set] == ["MEMBER-0001", "MEMBER-0002"]
    assert [row["sha256"] for row in source_set] == [
        hashlib.sha256((folder / name).read_bytes()).hexdigest()
        for name in ("a.pdf", "b.pdf")
    ]
    assert [row["study_id"] for row in source_set] == [
        f"UPLOAD-{row['sha256'][:20]}" for row in source_set
    ]


@pytest.mark.parametrize(
    ("case", "setup"),
    [
        ("empty", lambda folder: None),
        ("non_pdf", lambda folder: (folder / "notes.txt").write_text("not pdf")),
        (
            "duplicate_hash",
            lambda folder: (
                _write_pdf(folder, "a.pdf", b"same"),
                _write_pdf(folder, "b.pdf", b"same"),
            ),
        ),
    ],
)
def test_invalid_authorized_source_set_fails_before_any_project_write(
    tmp_path: Path, case: str, setup
) -> None:
    folder = tmp_path / "authorized"
    folder.mkdir()
    setup(folder)
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))

    with pytest.raises(fresh_bootstrap.FreshAgentBootstrapError):
        fresh_bootstrap._authorized_pdfs(folder)

    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert after == before


def test_invalid_archive_digest_is_stale_without_publishing_bytes(tmp_path: Path) -> None:
    folder = tmp_path / "authorized"
    folder.mkdir()
    pdf = _write_pdf(folder, "a.pdf", b"A")
    archive_path, _ = fresh_bootstrap._build_authorized_archive((pdf,), tmp_path)

    with pytest.raises(fresh_bootstrap.FreshAgentBootstrapError) as error:
        fresh_bootstrap._preflight_source_archive(archive_path, "0" * 64)

    assert error.value.code == "AUTHORIZED_PDF_STALE"
    assert not (tmp_path / "00_sources").exists()


def test_n3_preflight_matches_every_authorized_member_row(tmp_path: Path) -> None:
    folder = tmp_path / "authorized"
    folder.mkdir()
    _write_pdf(folder, "b.pdf", b"B")
    _write_pdf(folder, "a.pdf", b"A")
    _write_pdf(folder, "c.pdf", b"C")
    archive_path, source_set = fresh_bootstrap._build_authorized_archive(
        fresh_bootstrap._authorized_pdfs(folder), tmp_path
    )
    members = [
        {
            "member_id": row["member_id"],
            "member_display_name": row["name"],
            "name": row["name"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "download_id": row["download_id"],
            "source_id": row["source_id"],
            "study_id": row["study_id"],
        }
        for row in source_set
    ]
    expected_preflight = {
        "status": "awaiting_confirmation",
        "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "members": members,
        "role_options": ["MAIN", "SI"],
    }

    with patch.object(
        fresh_bootstrap,
        "_source_archive_preflight",
        return_value=expected_preflight,
    ):
        observed = fresh_bootstrap._preflight_source_archive(archive_path, source_set)

    assert observed is expected_preflight
    assert [row["member_id"] for row in observed["members"]] == [
        "MEMBER-0001",
        "MEMBER-0002",
        "MEMBER-0003",
    ]


def test_dashboard_source_pdf_descriptors_reuse_persisted_bundle_and_fail_closed_on_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    pdf = _write_pdf(project, "paper.pdf", b"stable")
    study_id = "study-1"
    source_id = "source-1"
    bundle = {
        "schema_version": "source-truth-bundle.v1",
        "project_id": project.name,
        "study_id": study_id,
        "sources": [
            {
                "source_id": source_id,
                "document_role": "MAIN",
                "page_count": 1,
                "pdf": {
                    "path": pdf.name,
                    "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                    "size_bytes": pdf.stat().st_size,
                },
            }
        ],
    }

    monkeypatch.setattr(dashboard, "project_dir", lambda _root, _id: project)
    monkeypatch.setattr(dashboard, "declared_study_ids", lambda _project: [study_id])
    monkeypatch.setattr(
        dashboard, "load_source_truth_bundle", lambda _project, _id: bundle
    )
    monkeypatch.setattr(
        source_truth, "load_source_truth_bundle", lambda _project, _id: bundle
    )
    monkeypatch.setattr(
        dashboard,
        "build_source_truth_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("descriptor read must not rebuild the source-truth bundle")
        ),
    )

    current = dashboard.project_source_pdf_descriptors_payload(tmp_path, project.name)
    assert current["status"] == "current"
    assert current["items"][0]["digest"] == bundle["sources"][0]["pdf"]["sha256"]

    pdf.write_bytes(b"drifted")
    stale = dashboard.project_source_pdf_descriptors_payload(tmp_path, project.name)
    assert stale == {"status": "stale", "items": []}


def test_dashboard_comparison_protocol_payload_uses_lightweight_new_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    (project / "01_evidence/source_truth").mkdir(parents=True)
    monkeypatch.setattr(dashboard, "project_dir", lambda _root, _id: project)
    monkeypatch.setattr(
        dashboard,
        "workflow_state",
        lambda _project: (_ for _ in ()).throw(
            AssertionError("new-route payload must not project full workflow")
        ),
    )
    monkeypatch.setattr(
        dashboard,
        "comparison_protocol_state",
        lambda _project: {
            "status": "needs_review",
            "workflow_can_continue": False,
            "reason_code": "COMPARISON_PROTOCOL_NOT_APPROVED",
            "value": {
                "comparison_id": "comparison-1",
                "protocol_digest": "p" * 64,
                "decision": None,
            },
        },
    )
    monkeypatch.setattr(
        dashboard,
        "paper_evidence_state",
        lambda _project: {"workflow_can_continue": True},
    )

    payload = dashboard.project_comparison_protocol_payload(tmp_path, project.name)

    assert payload["route"] == "evidence-to-release.v1"
    assert payload["evidence_ready"] is True


def test_malformed_pdf_is_rejected_before_project_write(tmp_path: Path) -> None:
    folder = tmp_path / "authorized"
    folder.mkdir()
    (folder / "malformed.pdf").write_bytes(b"%PDF-1.7\nnot a complete PDF")
    project_root = tmp_path / "projects" / "malformed-review"
    project_root.parent.mkdir()
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))

    with pytest.raises(fresh_bootstrap.FreshAgentBootstrapError) as error:
        fresh_bootstrap.FreshAgentBootstrap(project_root).start(
            topic="A bounded source-set review",
            authorized_pdf_folder=folder,
        )

    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert error.value.code == "SOURCE_ARCHIVE_PDF_INVALID"
    assert after == before
    assert not project_root.exists()


def test_fresh_bootstrap_persists_the_complete_authorized_source_set(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "authorized"
    folder.mkdir()
    _write_pdf(folder, "b.pdf", b"B")
    _write_pdf(folder, "a.pdf", b"A")
    project_root = tmp_path / "projects" / "source-set-review"
    project_root.parent.mkdir()

    def fake_preflight(archive_path: Path, expected_digests: object) -> dict[str, object]:
        with zipfile.ZipFile(archive_path) as archive:
            members = [
                {
                    "member_id": f"MEMBER-{index:04d}",
                    "member_display_name": name,
                    "sha256": hashlib.sha256(archive.read(name)).hexdigest(),
                }
                for index, name in enumerate(sorted(archive.namelist()), start=1)
            ]
        return {
            "status": "awaiting_confirmation",
            "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            "members": members,
            "source_set": members,
            "role_options": ["MAIN", "SI"],
        }

    with (
        patch.object(fresh_bootstrap, "_start_dashboard", return_value=("http://127.0.0.1:1", 1)),
        patch.object(fresh_bootstrap, "_preflight_source_archive", side_effect=fake_preflight),
        patch.object(
            fresh_bootstrap,
            "_publish_source_archive",
            side_effect=lambda project, archive, expected_digests, **kwargs: fake_preflight(
                archive, expected_digests
            ),
        ),
    ):
        result = fresh_bootstrap.FreshAgentBootstrap(project_root).start(
            topic="A bounded source-set review",
            authorized_pdf_folder=folder,
            rq="What source-bound evidence is available?",
            scope="the authorized source set",
            output_format="markdown",
        )

    review_state = json.loads(
        (project_root / "00_brief/review_state.json").read_text(encoding="utf-8")
    )
    assert review_state["brief"] == {
        "topic": "A bounded source-set review",
        "review_question": "What source-bound evidence is available?",
        "scope": "the authorized source set",
        "output_format": "markdown",
    }
    current = VersionContext.load(project_root).view_version(
        VersionContext.load(project_root).state().current_version_id
    )
    source_set = current.snapshot["agent_bootstrap"]["authorized_source_set"]
    assert [row["name"] for row in source_set] == ["a.pdf", "b.pdf"]
    assert result["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED


def test_n1_bootstrap_reaches_existing_dashboard_role_gate(tmp_path: Path) -> None:
    folder = tmp_path / "authorized"
    folder.mkdir()
    _write_pdf(folder, "a.pdf", b"A")
    project_root = tmp_path / "projects" / "n1-review"
    project_root.parent.mkdir()
    result: dict[str, object] | None = None

    try:
        result = fresh_bootstrap.FreshAgentBootstrap(project_root).start(
            topic="A bounded source-set review",
            authorized_pdf_folder=folder,
        )
        assert result["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
        assert result["project_id"] == "n1-review"
        assert result["current"]["revision"] == 1
        assert project_root.joinpath(
            "00_sources/manual_upload/inbox/source_bundle.zip"
        ).is_file()
    finally:
        if result is not None:
            fresh_bootstrap.FreshAgentBootstrap.stop_owned_dashboard(
                result["dashboard_pid"]
            )


@pytest.mark.parametrize(
    "reason_code",
    [
        "SOURCE_ROLE_INVALID",
        "SOURCE_ROLE_UNRESOLVED",
        "AUTHORIZED_PDF_STALE",
        "SOURCE_ARCHIVE_INVALID",
    ],
)
def test_source_role_or_stale_preflight_failure_is_zero_write(
    tmp_path: Path, reason_code: str
) -> None:
    folder = tmp_path / "authorized"
    folder.mkdir()
    _write_pdf(folder, "a.pdf", b"A")
    project_root = tmp_path / "projects" / "blocked-review"
    project_root.parent.mkdir()
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))

    def reject_preflight(*args: object, **kwargs: object) -> object:
        raise fresh_bootstrap.FreshAgentBootstrapError(reason_code)

    with patch.object(fresh_bootstrap, "_preflight_source_archive", side_effect=reject_preflight):
        with pytest.raises(fresh_bootstrap.FreshAgentBootstrapError) as error:
            fresh_bootstrap.FreshAgentBootstrap(project_root).start(
                topic="A bounded source-set review",
                authorized_pdf_folder=folder,
            )

    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert error.value.code == reason_code
    assert after == before
    assert not project_root.exists()


def test_n3_bootstrap_reaches_existing_dashboard_batch_role_gate(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "authorized"
    folder.mkdir()
    for name, payload in (("a.pdf", b"A"), ("b.pdf", b"B"), ("c.pdf", b"C")):
        _write_pdf(folder, name, payload)
    project_root = tmp_path / "projects" / "n3-review"
    project_root.parent.mkdir()
    result: dict[str, object] | None = None
    try:
        result = fresh_bootstrap.FreshAgentBootstrap(project_root).start(
            topic="A bounded source-set review",
            authorized_pdf_folder=folder,
        )
        assert result["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
        assert len(
            VersionContext.load(project_root)
            .view_version(VersionContext.load(project_root).state().current_version_id)
            .snapshot["agent_bootstrap"]["authorized_source_set"]
        ) == 3
        assert project_root.joinpath(
            "00_sources/manual_upload/inbox/source_bundle.zip"
        ).is_file()
    finally:
        if result is not None:
            fresh_bootstrap.FreshAgentBootstrap.stop_owned_dashboard(
                result["dashboard_pid"]
            )


def test_public_fresh_bootstrap_records_all_n10_authorized_members_and_hashes(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "authorized"
    folder.mkdir()
    for index in range(10):
        _write_pdf(folder, f"paper-{index:02d}.pdf", f"synthetic-{index}".encode())
    project_root = tmp_path / "projects" / "n10-public-review"
    project_root.parent.mkdir()
    result: dict[str, object] | None = None

    try:
        result = public_entry.start_or_resume_review(
            "A bounded N=10 source-set review",
            project_root,
            folder,
        )

        assert result["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
        context = VersionContext.load(project_root)
        current = context.view_version(context.state().current_version_id)
        source_set = current.snapshot["agent_bootstrap"]["authorized_source_set"]
        assert [row["name"] for row in source_set] == [
            f"paper-{index:02d}.pdf" for index in range(10)
        ]
        assert [row["sha256"] for row in source_set] == [
            hashlib.sha256(
                (folder / f"paper-{index:02d}.pdf").read_bytes()
            ).hexdigest()
            for index in range(10)
        ]

        archive_path = project_root / fresh_bootstrap.SOURCE_ARCHIVE_RELATIVE
        preflight = fresh_bootstrap._source_archive_preflight(archive_path)
        assert [member["name"] for member in preflight["members"]] == [
            f"paper-{index:02d}.pdf" for index in range(10)
        ]
        assert [member["sha256"] for member in preflight["members"]] == [
            row["sha256"] for row in source_set
        ]
    finally:
        if result is not None:
            fresh_bootstrap.FreshAgentBootstrap.stop_owned_dashboard(
                result["dashboard_pid"]
            )


def test_public_fresh_bootstrap_records_all_n20_members_hashes_and_study_boundaries(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "authorized"
    folder.mkdir()
    for index in range(20):
        _write_pdf(folder, f"paper-{index:02d}.pdf", f"synthetic-{index}".encode())
    project_root = tmp_path / "projects" / "n20-public-review"
    project_root.parent.mkdir()
    result: dict[str, object] | None = None

    try:
        result = public_entry.start_or_resume_review(
            "A bounded N=20 source-set review",
            project_root,
            folder,
        )

        assert result["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
        context = VersionContext.load(project_root)
        current = context.view_version(context.state().current_version_id)
        source_set = current.snapshot["agent_bootstrap"]["authorized_source_set"]
        expected_hashes = [
            hashlib.sha256(
                (folder / f"paper-{index:02d}.pdf").read_bytes()
            ).hexdigest()
            for index in range(20)
        ]
        expected_studies = [f"UPLOAD-{digest[:20]}" for digest in expected_hashes]

        assert len(source_set) == 20
        assert [row["name"] for row in source_set] == [
            f"paper-{index:02d}.pdf" for index in range(20)
        ]
        assert [row["sha256"] for row in source_set] == expected_hashes
        assert [row["source_id"] for row in source_set] == expected_studies
        assert [row["study_id"] for row in source_set] == expected_studies
        assert len({row["study_id"] for row in source_set}) == 20

        archive_path = project_root / fresh_bootstrap.SOURCE_ARCHIVE_RELATIVE
        preflight = fresh_bootstrap._source_archive_preflight(archive_path)
        assert len(preflight["members"]) == 20
        assert [member["sha256"] for member in preflight["members"]] == expected_hashes
        assert [member["source_id"] for member in preflight["members"]] == expected_studies
        assert [member["study_id"] for member in preflight["members"]] == expected_studies
        assert preflight["archive_sha256"] == hashlib.sha256(
            archive_path.read_bytes()
        ).hexdigest()
    finally:
        if result is not None:
            fresh_bootstrap.FreshAgentBootstrap.stop_owned_dashboard(
                result["dashboard_pid"]
            )


def test_n3_native_bootstrap_maps_all_members_through_real_dashboard(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "authorized"
    folder.mkdir()
    for name, payload in (("a.pdf", b"A"), ("b.pdf", b"B"), ("c.pdf", b"C")):
        _write_pdf(folder, name, payload)
    project_root = tmp_path / "projects" / "n3-native-dashboard"
    project_root.parent.mkdir()
    result: dict[str, object] | None = None

    def request(method: str, path: str, payload: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
        url = urlsplit(result["dashboard_url"])
        body = b"" if payload is None else json.dumps(payload).encode()
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
        try:
            connection.request(
                method,
                path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
            response = connection.getresponse()
            raw = response.read()
            try:
                decoded = json.loads(raw.decode())
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AssertionError(
                    f"non-JSON Dashboard response status={response.status} "
                    f"body={raw[:500]!r} request_bytes={len(body)}"
                ) from exc
            return response.status, decoded
        finally:
            connection.close()

    try:
        result = fresh_bootstrap.FreshAgentBootstrap(project_root).start(
            topic="A bounded source-set review",
            authorized_pdf_folder=folder,
        )
        project_id = result["project_id"]
        status, sources = request("GET", f"/api/project/{project_id}/sources")
        assert status == 200
        preflight = sources["preflight"]
        status, history = request("GET", f"/api/project/{project_id}/history")
        assert status == 200
        rows = [
            {
                "member_id": member["member_id"],
                "name": member["name"],
                "sha256": member["sha256"],
                "download_id": member["download_id"],
                "source_id": member["source_id"],
                "study_id": member["study_id"],
                "document_role": "MAIN" if index == 0 else "SI",
            }
            for index, member in enumerate(preflight["members"])
        ]
        status, mapped = request(
            "POST",
            f"/api/project/{project_id}/source-mapping",
            {
                "members": rows,
                "archive_sha256": preflight["archive_sha256"],
                "expected_revision": history["revision"],
            },
        )
        assert status == 200
        assert mapped["status"] == "mapped"
        status, selected = request("GET", f"/api/project/{project_id}/sources")
        assert status == 200
        assert len(selected["sources"]) == 3
        assert {source["currentness"] for source in selected["sources"]} == {"current"}
    finally:
        if result is not None:
            fresh_bootstrap.FreshAgentBootstrap.stop_owned_dashboard(
                result["dashboard_pid"]
            )


def test_cold_process_restart_resumes_with_loopback_dashboard(tmp_path: Path) -> None:
    folder = tmp_path / "authorized"
    folder.mkdir()
    _write_pdf(folder, "main.pdf", b"cold-resume")
    project_root = tmp_path / "projects" / "cold-resume"
    project_root.parent.mkdir()

    start_script = """
import json
import sys
from pathlib import Path
from review_writer.agent import fresh_bootstrap

project = Path(sys.argv[1])
folder = Path(sys.argv[2])
result = fresh_bootstrap.FreshAgentBootstrap(project).start(
    topic="Cold resume health probe",
    authorized_pdf_folder=folder,
)
pid = result["dashboard_pid"]
fresh_bootstrap.FreshAgentBootstrap.stop_owned_dashboard(pid)
print(json.dumps({"dashboard_url": result["dashboard_url"], "dashboard_pid": pid}))
"""
    started = _run_child_json(start_script, project_root, folder)
    assert isinstance(started["dashboard_url"], str)
    assert started["dashboard_url"].startswith("http://127.0.0.1:")
    assert isinstance(started["dashboard_pid"], int)
    assert started["dashboard_pid"] > 0

    resume_script = """
import json
import sys
from pathlib import Path
from review_writer.agent import fresh_bootstrap, public_entry

project = Path(sys.argv[1])
folder = Path(sys.argv[2])
result = public_entry.start_or_resume_review(
    "Cold resume health probe",
    project,
    folder,
)
pid = result.get("dashboard_pid")
try:
    print(json.dumps({
        "result": result.get("result"),
        "status": result.get("status"),
        "reason_code": result.get("reason_code"),
        "dashboard_url": result.get("dashboard_url"),
        "dashboard_pid": pid,
    }))
finally:
    if isinstance(pid, int):
        fresh_bootstrap.FreshAgentBootstrap.stop_owned_dashboard(pid)
"""
    resumed = _run_child_json(resume_script, project_root, folder)
    assert resumed["result"] == "RESUMED"
    assert resumed["status"] != "HOLD"
    assert isinstance(resumed["dashboard_url"], str)
    assert resumed["dashboard_url"].startswith("http://127.0.0.1:")
    assert isinstance(resumed["dashboard_pid"], int)
    assert resumed["dashboard_pid"] > 0


def test_public_n3_mapping_resume_reaches_parse_quality_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public N=3 source mapping must continue into local parse quality."""
    # WSL process startup can exceed the production health-check budget under
    # a cold pytest interpreter; keep this real Dashboard integration test
    # deterministic without changing the product timeout.
    monkeypatch.setattr(fresh_bootstrap, "_DASHBOARD_START_TIMEOUT_SECONDS", 10.0)
    # The chain below exercises the real HTTP seam; keep the public adapter on
    # this owned Dashboard URL so a 0.2s health probe cannot spawn an
    # unowned replacement while the integration test is doing source work.
    monkeypatch.setattr(public_entry, "_dashboard_is_healthy", lambda _value: True)
    folder = tmp_path / "authorized"
    folder.mkdir()
    for name, payload in (("a.pdf", b"A"), ("b.pdf", b"B"), ("c.pdf", b"C")):
        _write_pdf(folder, name, payload)
    project_root = tmp_path / "projects" / "n3-public-parse"
    project_root.parent.mkdir()
    result: dict[str, object] | None = None

    def request(
        base_url: str,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
        url = urlsplit(base_url)
        body = b"" if payload is None else json.dumps(payload).encode()
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
        try:
            connection.request(
                method,
                path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
            response = connection.getresponse()
            raw = response.read()
            try:
                decoded = json.loads(raw.decode())
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AssertionError(
                    f"non-JSON Dashboard response status={response.status} "
                    f"body={raw[:500]!r} request_bytes={len(body)}"
                ) from exc
            return response.status, decoded
        finally:
            connection.close()

    try:
        result = run_agent(
            "A bounded N=3 source-set review",
            project_root,
            folder,
        )
        assert result["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
        project_id = result["project_id"]
        dashboard_url = result["dashboard_url"]

        def adopt_dashboard_url(response: dict[str, object]) -> None:
            nonlocal dashboard_url
            candidate = response.get("dashboard_url")
            if isinstance(candidate, str) and candidate:
                dashboard_url = candidate

        status, sources = request(dashboard_url, "GET", f"/api/project/{project_id}/sources")
        assert status == 200
        preflight = sources["preflight"]
        status, history = request(dashboard_url, "GET", f"/api/project/{project_id}/history")
        assert status == 200
        rows = [
            {
                "member_id": member["member_id"],
                "name": member["name"],
                "sha256": member["sha256"],
                "download_id": member["download_id"],
                "source_id": member["source_id"],
                "study_id": member["study_id"],
                # Treat each synthetic PDF as one independent study's MAIN
                # article so the public parse-quality set is complete.
                "document_role": "MAIN",
            }
            for member in preflight["members"]
        ]
        status, mapped = request(
            dashboard_url,
            "POST",
            f"/api/project/{project_id}/source-mapping",
            {
                "members": rows,
                "archive_sha256": preflight["archive_sha256"],
                "expected_revision": history["revision"],
            },
        )
        assert status == 200
        assert mapped["status"] == "mapped"

        resumed = run_agent(
            "A bounded N=3 source-set review",
            project_root,
            folder,
        )
        adopt_dashboard_url(resumed)
        assert resumed["result"] == "RESUMED"
        assert resumed["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
        assert resumed["reason_code"] == "PARSE_QUALITY_HUMAN_ACTION_REQUIRED"
        assert resumed["revision"] > result["revision"]
        current = VersionContext.load(project_root).view_version(
            VersionContext.load(project_root).state().current_version_id
        )
        parse = current.snapshot["agent_parse"]
        assert parse["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
        assert parse["reason_code"] == "PARSE_QUALITY_HUMAN_ACTION_REQUIRED"
        assert parse["source_count"] == 3

        # Before the human gate is complete, public resume must preserve the
        # parse current and leave Evidence storage untouched.
        before_unapproved = VersionContext.load(project_root).state()
        unapproved_candidates = sorted(
            project_root.glob("01_evidence/**/paper_evidence_candidates.json")
        )
        unapproved = run_agent(
            "A bounded N=3 source-set review",
            project_root,
            folder,
        )
        adopt_dashboard_url(unapproved)
        after_unapproved = VersionContext.load(project_root).state()
        assert unapproved["reason_code"] == "PARSE_QUALITY_HUMAN_ACTION_REQUIRED"
        assert unapproved["write_mode"] == "NONE"
        assert after_unapproved.revision == before_unapproved.revision
        assert sorted(project_root.glob("01_evidence/**/paper_evidence_candidates.json")) == unapproved_candidates

        # Close every actionable parse-quality object through the real HTTP
        # Dashboard seam, then resume the public Agent flow.  Evidence should
        # be materialized from the approved source-bound parse, not by a test
        # helper or a second store.
        status, quality = request(
            dashboard_url, "GET", f"/api/project/{project_id}/parse-quality"
        )
        assert status == 200
        for study in quality["studies"]:
            for obj in study["objects"]:
                actions = obj["actions"]
                if not actions:
                    continue
                action = (
                    "approve_candidate_extraction"
                    if "approve_candidate_extraction" in actions
                    else "pdf_locator_only"
                )
                decision = {
                    "study_id": study["study_id"],
                    "object_id": obj["object_id"],
                    "decision_token": obj["decision_token"],
                    "action": action,
                    "note": "Synthetic integration review completed against the source PDF.",
                }
                if action == "pdf_locator_only":
                    source = next(
                        row
                        for row in sources["sources"]
                        if row["study_id"] == study["study_id"]
                    )
                    decision["pdf_resolution"] = {
                        "pages": [1],
                        "source_scope": "Page 1 only",
                        "limitations": "Synthetic PDF locator-only review.",
                        "source_pdf_sha256": source["source_pdf_sha256"],
                    }
                status, saved = request(
                    dashboard_url,
                    "PUT",
                    f"/api/project/{project_id}/parse-quality",
                    decision,
                )
                assert status == 200
                quality = saved
        assert quality["workflow_can_continue"] is True

        resumed = run_agent(
            "A bounded N=3 source-set review",
            project_root,
            folder,
        )
        adopt_dashboard_url(resumed)
        assert resumed["result"] == "RESUMED"
        assert resumed["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
        assert resumed["reason_code"] == "PAPER_EVIDENCE_HUMAN_ACTION_REQUIRED"
        evidence = local_pdf_parse.paper_evidence_state(project_root)
        assert evidence["total_count"] == 3
        assert evidence["workflow_can_continue"] is False
        assert all(
            row["locator"]["source_mode"] == "parsed_candidate"
            for row in evidence["rows"]
        )

        # A second resume after materialization is a true idempotent no-write.
        candidate_bytes = {
            path: path.read_bytes()
            for path in project_root.glob("01_evidence/**/paper_evidence_candidates.json")
        }
        before_idempotent = VersionContext.load(project_root).state()
        repeated = run_agent(
            "A bounded N=3 source-set review",
            project_root,
            folder,
        )
        adopt_dashboard_url(repeated)
        after_idempotent = VersionContext.load(project_root).state()
        assert repeated["reason_code"] == "PAPER_EVIDENCE_HUMAN_ACTION_REQUIRED"
        assert repeated["write_mode"] == "NONE"
        assert after_idempotent.revision == before_idempotent.revision
        assert {
            path: path.read_bytes()
            for path in project_root.glob("01_evidence/**/paper_evidence_candidates.json")
        } == candidate_bytes

        # Approve every candidate through the real Paper Evidence Dashboard
        # seam, then public resume should create the next synthesis candidate.
        status, paper = request(
            dashboard_url, "GET", f"/api/project/{project_id}/paper-evidence"
        )
        assert status == 200
        assert len(paper["items"]) == 3
        for item in paper["items"]:
            status, paper = request(
                dashboard_url,
                "PUT",
                f"/api/project/{project_id}/paper-evidence",
                {
                    "evidence_id": item["evidence_id"],
                    "version_token": item["version_token"],
                    "action": "approve",
                    "reason": "Synthetic source-bound evidence review completed.",
                },
            )
            assert status == 200
        assert paper["summary"]["approved_count"] == 3
        continued = run_agent(
            "A bounded N=3 source-set review",
            project_root,
            folder,
        )
        adopt_dashboard_url(continued)
        assert continued["result"] == "RESUMED"
        assert continued["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
        assert continued["reason_code"] == "SYNTHESIS_PROTOCOL_HUMAN_ACTION_REQUIRED"
        status, protocol = request(
            dashboard_url, "GET", f"/api/project/{project_id}/comparison-protocol"
        )
        assert status == 200
        assert protocol["protocol"]["comparison_id"]
        assert protocol["workflow_can_continue"] is False

        status, protocol = request(
            dashboard_url, "PUT", f"/api/project/{project_id}/comparison-protocol",
            {
                "version_token": protocol["protocol"]["version_token"],
                "action": "approve",
                "reason": "Synthetic comparison protocol review completed.",
            },
        )
        assert status == 200
        assert protocol["workflow_can_continue"] is True

        continued = run_agent(
            "A bounded N=3 source-set review", project_root, folder
        )
        adopt_dashboard_url(continued)
        assert continued["reason_code"] == "SYNTHESIS_CLAIM_HUMAN_ACTION_REQUIRED"
        status, synthesis = request(
            dashboard_url, "GET", f"/api/project/{project_id}/synthesis"
        )
        assert status == 200
        assert len(synthesis["items"]) == 1
        assert synthesis["items"][0]["status"] == "needs_review"

        item = synthesis["items"][0]
        status, synthesis = request(
            dashboard_url, "PUT", f"/api/project/{project_id}/synthesis",
            {
                "synthesis_id": item["synthesis_id"],
                "version_token": item["version_token"],
                "action": "approve",
                "reason": "Synthetic synthesis claim review completed.",
            },
        )
        assert status == 200
        assert synthesis["workflow_can_continue"] is True

        continued = run_agent(
            "A bounded N=3 source-set review", project_root, folder
        )
        adopt_dashboard_url(continued)
        assert continued["reason_code"] == "SECTION_CONTRACT_HUMAN_ACTION_REQUIRED"
        status, contracts = request(
            dashboard_url, "GET", f"/api/project/{project_id}/section-contracts"
        )
        assert status == 200
        assert len(contracts["items"]) == 1
        assert contracts["items"][0]["status"] == "needs_review"

        contract = contracts["items"][0]
        status, contracts = request(
            dashboard_url, "PUT", f"/api/project/{project_id}/section-contracts",
            {
                "section_id": contract["section_id"],
                "version_token": contract["version_token"],
                "action": "approve",
                "reason": "Synthetic section contract review completed.",
            },
        )
        assert status == 200
        assert contracts["workflow_can_continue"] is True

        continued = run_agent(
            "A bounded N=3 source-set review", project_root, folder
        )
        adopt_dashboard_url(continued)
        assert continued["reason_code"] == "SECTION_DRAFT_HUMAN_ACTION_REQUIRED"

        # Approve the v1 candidate through the public Dashboard draft seam.
        # This is the same payload a human researcher submits from GET /draft;
        # no direct manuscript or VersionContext helper is allowed here.
        status, draft = request(
            dashboard_url, "GET", f"/api/project/{project_id}/draft"
        )
        assert status == 200
        assert draft["route"] == "evidence-to-release.v1"
        assert len(draft["sections"]) == 1
        v1_section = draft["sections"][0]
        assert v1_section["status"] == "needs_human_edit"
        edited_body = v1_section["body"].replace(
            "approved source-bound", "source-bound", 1
        )
        assert edited_body != v1_section["body"]
        status, draft = request(
            dashboard_url,
            "PUT",
            f"/api/project/{project_id}/draft",
            {
                "section_id": v1_section["section_id"],
                "version_token": v1_section["version_token"],
                "edited_body": edited_body,
                "reason": "Human researcher checked the source-bound wording.",
                "actor_type": "human_researcher",
                "actor_label": "研究者",
            },
        )
        assert status == 200
        assert draft["sections"][0]["status"] == "approved"
        assert draft["sections"][0]["decision"]["actor_type"] == "human_researcher"

        # Public resume must consume that persisted decision and advance the
        # same generator session to a v2 candidate.
        continued = run_agent(
            "A bounded N=3 source-set review", project_root, folder
        )
        adopt_dashboard_url(continued)
        assert continued["result"] == "RESUMED"
        assert continued["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
        assert continued["candidate"]["version"] == "v2"
        assert continued["current"]["version_id"].startswith("generator-v2-")

        # Register the researcher-owned synthesis placeholder through the
        # public review-figures seam.  The registration response is the sole
        # source of the placeholder brief and optimistic-concurrency token.
        status, figures = request(
            dashboard_url, "GET", f"/api/project/{project_id}/review-figures"
        )
        assert status == 200
        registration = figures["placeholder_registration"]
        assert registration["next_action"] == "HUMAN_ACTION_REQUIRED"
        placeholder = registration["placeholder"]
        status, figures = request(
            dashboard_url,
            "PUT",
            f"/api/project/{project_id}/review-figures",
            {
                "action": "register_placeholder",
                "placeholder": placeholder,
                "version_token": registration["version_token"],
                "actor_type": "human_researcher",
                "actor_label": "研究者",
            },
        )
        assert status == 200
        assert figures["placeholder_registration"] is None
        assert figures["summary"]["placeholder_count"] == 1

        # Bind the complete placeholder brief into the v2 manuscript through
        # the public Dashboard draft seam.  Release validation requires the
        # marker, scientific question, and every panel task to be visible in
        # the authoritative manuscript body.
        status, draft = request(
            dashboard_url, "GET", f"/api/project/{project_id}/draft"
        )
        assert status == 200
        assert draft["route"] == "evidence-to-release.v1"
        assert len(draft["sections"]) == 1
        v2_section = draft["sections"][0]
        assert v2_section["status"] == "needs_human_edit"
        panel_tasks = [
            panel["task"]
            for panel in placeholder["panels"]
            if isinstance(panel, dict) and isinstance(panel.get("task"), str)
        ]
        brief_lines = [
            "<!-- SYNTHESIS_FIGURE_PLACEHOLDER: "
            f"{placeholder['placeholder_id']} | {placeholder['scientific_question']} | "
            f"{' | '.join(panel_tasks)} -->"
        ]
        edited_body = f"{v2_section['body'].rstrip()}\n\n" + "\n".join(brief_lines)
        status, draft = request(
            dashboard_url,
            "PUT",
            f"/api/project/{project_id}/draft",
            {
                "section_id": v2_section["section_id"],
                "version_token": v2_section["version_token"],
                "edited_body": edited_body,
                "reason": "研究者核对并绑定综合图占位符简述。",
                "actor_type": "human_researcher",
                "actor_label": "研究者",
            },
        )
        assert status == 200
        assert draft["sections"][0]["status"] == "approved"

        # The complete public flow must now publish a bounded self-reviewed
        # Markdown/DOCX pair.  Keep this assertion intentionally strict so a
        # missing release input remains an observed first blocker.
        released = run_agent(
            "A bounded N=3 source-set review", project_root, folder
        )
        adopt_dashboard_url(released)
        assert released["release_status"] == "SELF_REVIEWED_DRAFT"
        assert released["release"]["markdown_path"] == "05_release/self_reviewed_draft.md"
        assert released["release"]["docx_path"] == "05_release/self_reviewed_draft.docx"
        assert (project_root / released["release"]["markdown_path"]).is_file()
        assert (project_root / released["release"]["docx_path"]).is_file()

        def project_fingerprint() -> dict[str, tuple[bytes, int, int]]:
            return {
                path.relative_to(project_root).as_posix(): (
                    path.read_bytes(),
                    path.stat().st_mtime_ns,
                    path.stat().st_ino,
                )
                for path in sorted(project_root.rglob("*"))
                if path.is_file() and not path.is_symlink()
            }

        authoritative_markdown = project_root / "04_manuscript/manuscript.md"
        authoritative_bytes = authoritative_markdown.read_bytes()
        release_markdown = project_root / released["release"]["markdown_path"]
        release_docx = project_root / released["release"]["docx_path"]
        before_stale_fingerprint = project_fingerprint()
        before_stale_context = VersionContext.load(project_root).state()
        release_markdown.write_bytes(
            release_markdown.read_bytes()
            + b"\n\nSTALE RELEASE MUTATION FOR INTEGRATION TEST\n"
        )
        assert project_fingerprint() != before_stale_fingerprint

        direct_stale = run_agent(
            "A bounded N=3 source-set review", project_root, folder
        )
        assert direct_stale["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
        assert direct_stale["reason_code"] == "RELEASE_OUTDATED"
        assert direct_stale["release_status"] == "RELEASE_OUTDATED"
        assert direct_stale["write_mode"] == "NONE"
        assert "candidate" not in direct_stale
        assert direct_stale["next_action"] == {
            "project_id": project_id,
            "route": "/final",
            "type": "REGENERATE_RELEASE",
            "reason_code": "RELEASE_OUTDATED",
        }
        stale_fingerprint = project_fingerprint()
        stale_context = VersionContext.load(project_root).state()
        assert stale_context == before_stale_context

        stale_resume = run_agent(
            "A bounded N=3 source-set review", project_root, folder
        )
        adopt_dashboard_url(stale_resume)
        assert stale_resume["release_status"] == "RELEASE_OUTDATED"
        assert stale_resume["write_mode"] == "NONE"
        assert stale_resume["next_action"] == {
            "project_id": project_id,
            "route": "/final",
            "type": "REGENERATE_RELEASE",
            "reason_code": "RELEASE_OUTDATED",
        }
        assert project_fingerprint() == stale_fingerprint
        assert VersionContext.load(project_root).state() == stale_context

        status, final = request(
            dashboard_url, "GET", f"/api/project/{project_id}/final"
        )
        assert status == 200
        assert final["release_status"] == "RELEASE_OUTDATED"
        assert final["release_snapshot"] == {
            "exists": True,
            "matches_authoritative": False,
            "integrity_valid": False,
            "docx_exists": False,
        }
        assert final["final_draft_docx_exists"] is False
        assert final["final_draft_docx_path"] == ""

        status, exported = request(
            dashboard_url,
            "POST",
            f"/api/project/{project_id}/export-docx",
            {"release_level": "SELF_REVIEWED_DRAFT"},
        )
        assert status == 200
        assert exported["ok"] is True
        assert exported["release_status"] == "SELF_REVIEWED_DRAFT"
        assert exported["release_level"] == "SELF_REVIEWED_DRAFT"
        assert release_markdown.read_bytes() == authoritative_bytes
        assert release_docx.is_file()

        status, final = request(
            dashboard_url, "GET", f"/api/project/{project_id}/final"
        )
        assert status == 200
        assert final["release_status"] == "SELF_REVIEWED_DRAFT"
        assert final["manuscript_source"] == "release_snapshot"
        assert final["release_snapshot"] == {
            "exists": True,
            "matches_authoritative": True,
            "integrity_valid": True,
            "docx_exists": True,
        }
        assert final["final_draft_docx_path"] == (
            f"{project_id}/05_release/self_reviewed_draft.docx"
        )
        assert final["final_draft_docx_exists"] is True
    finally:
        if result is not None:
            fresh_bootstrap.FreshAgentBootstrap.stop_owned_dashboard(
                result["dashboard_pid"]
            )


def test_local_parse_counts_main_studies_when_receipt_has_si_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MAIN bundles/gates are not compared against SI-only study rows."""
    project = tmp_path / "parse-project"
    (project / "00_brief").mkdir(parents=True)
    (project / "00_brief/review_state.json").write_text(
        json.dumps({"project_id": project.name}), encoding="utf-8"
    )
    (project / ".paper_evidence.lock").write_bytes(b"lock")
    source_root = project / "00_sources"
    source_root.mkdir()
    main = _write_pdf(source_root, "main.pdf", b"main")
    si = _write_pdf(source_root, "si.pdf", b"supplement")
    si_only = _write_pdf(source_root, "si-only.pdf", b"supplement-only")
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    receipt = {
        "schema_version": "acquisition-final-receipt.v1",
        "studies": [
            {
                "study_id": "study-main",
                "source_id": "source-main",
                "main_pdf": {"path": "main.pdf", "sha256": digest(main)},
                "si_pdf": {"path": "si.pdf", "sha256": digest(si)},
            },
            {
                "study_id": "study-si-only",
                "source_id": "source-si-only",
                "si_pdf": {"path": "si-only.pdf", "sha256": digest(si_only)},
            },
        ],
    }
    (source_root / "acquisition_final_receipt.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    VersionContext.create(
        {"currentness": "current"},
        project_id=project.name,
        project_root=project,
    )
    parse_rows = [
        {
            "slug": "main",
            "state": "done",
            "study_id": "study-main",
            "source_id": "source-main",
            "document_role": "MAIN",
            "relative_pdf_path": "main.pdf",
            "source_pdf_sha256": digest(main),
        },
        {
            "slug": "si",
            "state": "done",
            "study_id": "study-main",
            "source_id": "source-main__SI",
            "document_role": "SI",
            "relative_pdf_path": "si.pdf",
            "source_pdf_sha256": digest(si),
        },
        {
            "slug": "si-only",
            "state": "done",
            "study_id": "study-si-only",
            "source_id": "source-si-only__SI",
            "document_role": "SI",
            "relative_pdf_path": "si-only.pdf",
            "source_pdf_sha256": digest(si_only),
        },
    ]
    monkeypatch.setattr(
        local_pdf_parse,
        "_write_mineru_parse_output",
        lambda _evidence, _rows: (parse_rows, [
            {"source_id": row["source_id"], "source_pdf_sha256": row["source_pdf_sha256"]}
            for row in parse_rows
        ]),
    )
    monkeypatch.setattr(
        local_pdf_parse,
        "write_source_truth_bundle",
        lambda _project, _study_id: {
            "study_id": "study-main",
            "bundle_digest": "b" * 64,
            "sources": [
                {
                    "source_id": "source-main",
                    "document_role": "MAIN",
                    "pdf": {"path": "00_sources/main.pdf", "sha256": digest(main)},
                }
            ],
        },
    )
    monkeypatch.setattr(
        local_pdf_parse,
        "write_parse_quality_gate",
        lambda _project, _study_id: {
            "study_id": "study-main",
            "gate_digest": "g" * 64,
            "status": "needs_review",
        },
    )
    monkeypatch.setattr(
        local_pdf_parse,
        "_build_staged_figure_candidates",
        lambda *_args, **_kwargs: {
            "schema_version": "review-writer.agent-figure-candidates.v1",
            "project_id": project.name,
            "status": "gap",
            "parser_mode": "MINERU",
            "figures": [],
            "gaps": [],
        },
    )
    monkeypatch.setattr(local_pdf_parse, "_publish_components", lambda *_args: None)

    result = local_pdf_parse.parse_project_sources(project)

    assert result["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
    assert result["reason_code"] == "PARSE_QUALITY_HUMAN_ACTION_REQUIRED"
    assert [row["study_id"] for row in result["source_truth"]] == ["study-main"]
