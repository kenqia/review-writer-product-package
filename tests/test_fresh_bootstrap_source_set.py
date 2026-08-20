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

from review_writer.agent import fresh_bootstrap
from review_writer.product_foundation import VersionContext


def _write_pdf(folder: Path, name: str, payload: bytes) -> Path:
    path = folder / name
    body = b"%PDF-1.7\n% " + payload + b"\n1 0 obj\n<< /Length 0 >>\nstream\nendstream\nendobj\n"
    xref_offset = len(body)
    object_offset = len(b"%PDF-1.7\n% ") + len(payload) + 1
    path.write_bytes(
        body
        + b"xref\n0 2\n0000000000 65535 f \n"
        + f"{object_offset:010d}".encode()
        + b" 00000 n \n"
        + b"trailer\n<< /Size 2 >>\nstartxref\n"
        + str(xref_offset).encode()
        + b"\n%%EOF\n"
    )
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
        )

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
