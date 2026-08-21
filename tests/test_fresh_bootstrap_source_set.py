"""Source-set contract tests for the native fresh Agent bootstrap."""

from __future__ import annotations

import hashlib
import http.client
import json
from urllib.parse import urlsplit
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from review_writer.agent import fresh_bootstrap, local_pdf_parse, public_entry
from review_writer.product_foundation import VersionContext


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
            return response.status, json.loads(response.read().decode())
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


def test_public_n3_mapping_resume_reaches_parse_quality_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public N=3 source mapping must continue into local parse quality."""
    # WSL process startup can exceed the production health-check budget under
    # a cold pytest interpreter; keep this real Dashboard integration test
    # deterministic without changing the product timeout.
    monkeypatch.setattr(fresh_bootstrap, "_DASHBOARD_START_TIMEOUT_SECONDS", 10.0)
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
            return response.status, json.loads(response.read().decode())
        finally:
            connection.close()

    try:
        result = public_entry.start_or_resume_review(
            "A bounded N=3 source-set review",
            project_root,
            folder,
        )
        assert result["status"] == fresh_bootstrap.HUMAN_ACTION_REQUIRED
        project_id = result["project_id"]
        dashboard_url = result["dashboard_url"]

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

        resumed = public_entry.start_or_resume_review(
            "A bounded N=3 source-set review",
            project_root,
            folder,
        )
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
        unapproved = public_entry.start_or_resume_review(
            "A bounded N=3 source-set review",
            project_root,
            folder,
        )
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

        resumed = public_entry.start_or_resume_review(
            "A bounded N=3 source-set review",
            project_root,
            folder,
        )
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
        repeated = public_entry.start_or_resume_review(
            "A bounded N=3 source-set review",
            project_root,
            folder,
        )
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
        continued = public_entry.start_or_resume_review(
            "A bounded N=3 source-set review",
            project_root,
            folder,
        )
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

        continued = public_entry.start_or_resume_review(
            "A bounded N=3 source-set review", project_root, folder
        )
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

        continued = public_entry.start_or_resume_review(
            "A bounded N=3 source-set review", project_root, folder
        )
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

        continued = public_entry.start_or_resume_review(
            "A bounded N=3 source-set review", project_root, folder
        )
        assert continued["reason_code"] == "SECTION_DRAFT_HUMAN_ACTION_REQUIRED"
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
