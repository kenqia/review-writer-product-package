from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PARSER_PATH = ROOT / "skills/mineru-precise-parse-review-writer/scripts/parse_review_writer_pdfs.py"


def _module():
    spec = importlib.util.spec_from_file_location("review_writer_mineru_parser", PARSER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _archive_bytes() -> bytes:
    v1 = [{"type": "text", "text": "MinerU output", "page_idx": 0, "bbox": [0, 0, 1, 1]}]
    v2 = [[dict(v1[0])]]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("full.md", "# MinerU output\n\n![figure](images/figure.png)\n")
        archive.writestr("paper_content_list.json", json.dumps(v1))
        archive.writestr("paper_content_list_v2.json", json.dumps(v2))
        archive.writestr("images/figure.png", b"PNG")
    return buffer.getvalue()


class _Response:
    def __init__(self, payload=None, content: bytes = b""):
        self._payload = payload
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1024):
        del chunk_size
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Session:
    def __init__(self, data_id: str):
        self.data_id = data_id
        self.uploaded = False
        self.downloaded = False
        self.verify = None
        self.download_verify = None
        self.request_verifies = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, *_args, **_kwargs):
        self.request_verifies.append(_kwargs.get("verify"))
        return _Response({"code": 0, "data": {"batch_id": "batch-1", "file_urls": ["https://upload"]}})

    def put(self, *_args, **_kwargs):
        self.request_verifies.append(_kwargs.get("verify"))
        self.uploaded = True
        return _Response({"code": 0})

    def get(self, url, *_args, **_kwargs):
        self.request_verifies.append(_kwargs.get("verify"))
        if url == "https://download":
            self.downloaded = True
            self.download_verify = self.verify
            return _Response(content=_archive_bytes())
        return _Response(
            {
                "code": 0,
                "data": {
                    "extract_result": [
                        {
                            "data_id": self.data_id,
                            "state": "done",
                            "full_zip_url": "https://download",
                        }
                    ]
                },
            }
        )


def test_mineru_api_adapter_materializes_local_parser_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = _module()
    pdf_dir = tmp_path / "authorized"
    pdf_dir.mkdir()
    pdf = pdf_dir / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    output = tmp_path / "parser-output"
    session = _Session(f"rw-{module._sha256(pdf)[:24]}")
    monkeypatch.setenv("MINERU_API_TOKEN", "test-token-not-real")
    monkeypatch.setattr(module.requests, "Session", lambda: session)
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: pytest.fail("download must reuse the API session"))

    assert module.main(
        [
            "--pdf", str(pdf),
            "--input-dir", str(pdf_dir),
            "--output-dir", str(output),
            "--poll-interval", "1",
            "--timeout-minutes", "1",
        ]
    ) == 0
    assert session.uploaded
    assert session.downloaded
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    row = manifest["completed"][0]
    assert row["state"] == "done"
    assert row["pdf_name"] == "paper.pdf"
    assert (output / "markdown" / f"{row['slug']}.md").is_file()
    extracted = output / "extracted" / row["slug"]
    assert (extracted / "layout.json").is_file()
    assert (extracted / f"{row['slug']}_content_list_v2.json").is_file()
    assert (extracted / "images/figure.png").read_bytes() == b"PNG"


def test_mineru_ca_bundle_is_applied_to_reused_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    module = _module()
    pdf_dir = tmp_path / "authorized"
    pdf_dir.mkdir()
    pdf = pdf_dir / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    output = tmp_path / "parser-output"
    ca_bundle = tmp_path / "approved-ca.pem"
    ca_bundle.write_text("not-a-real-cert-for-test", encoding="utf-8")
    session = _Session(f"rw-{module._sha256(pdf)[:24]}")
    monkeypatch.setenv("MINERU_API_TOKEN", "test-token-not-real")
    monkeypatch.setenv("REVIEW_WRITER_MINERU_CA_BUNDLE", str(ca_bundle))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "ignored-by-dedicated-setting.pem"))
    monkeypatch.setattr(module.requests, "Session", lambda: session)

    assert module.main(
        [
            "--pdf", str(pdf),
            "--input-dir", str(pdf_dir),
            "--output-dir", str(output),
            "--poll-interval", "1",
            "--timeout-minutes", "1",
        ]
    ) == 0
    assert session.verify == str(ca_bundle)
    assert session.download_verify == str(ca_bundle)
    assert session.request_verifies == [str(ca_bundle)] * 4


def test_requests_ca_bundle_is_supported_when_dedicated_setting_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    module = _module()
    pdf_dir = tmp_path / "authorized"
    pdf_dir.mkdir()
    pdf = pdf_dir / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    output = tmp_path / "parser-output"
    ca_bundle = tmp_path / "requests-ca.pem"
    ca_bundle.write_text("not-a-real-cert-for-test", encoding="utf-8")
    session = _Session(f"rw-{module._sha256(pdf)[:24]}")
    monkeypatch.setenv("MINERU_API_TOKEN", "test-token-not-real")
    monkeypatch.delenv("REVIEW_WRITER_MINERU_CA_BUNDLE", raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_bundle))
    monkeypatch.setattr(module.requests, "Session", lambda: session)

    assert module.main(
        [
            "--pdf", str(pdf),
            "--input-dir", str(pdf_dir),
            "--output-dir", str(output),
            "--poll-interval", "1",
            "--timeout-minutes", "1",
        ]
    ) == 0
    assert session.verify == str(ca_bundle)


def test_invalid_mineru_ca_bundle_is_zero_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    module = _module()
    pdf_dir = tmp_path / "authorized"
    pdf_dir.mkdir()
    pdf = pdf_dir / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    output = tmp_path / "parser-output"
    monkeypatch.setenv("MINERU_API_TOKEN", "test-token-not-real")
    monkeypatch.setenv("REVIEW_WRITER_MINERU_CA_BUNDLE", str(tmp_path / "missing-ca.pem"))

    with pytest.raises(module.MinerUAdapterError, match="MINERU_CA_BUNDLE_INVALID"):
        module.main(["--pdf", str(pdf), "--input-dir", str(pdf_dir), "--output-dir", str(output)])
    assert not output.exists()
    assert not list(tmp_path.glob(f".{output.name}.*"))


def test_mineru_tls_failure_is_actionable_and_zero_write(tmp_path: Path):
    module = _module()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    output = tmp_path / "parser-output"

    class _TlsFailureSession:
        verify = None

        def get(self, *_args, **_kwargs):
            raise module.requests.exceptions.SSLError("certificate verify failed")

    with pytest.raises(module.MinerUAdapterError, match="MINERU_RESULT_DOWNLOAD_TLS_HOLD") as error:
        module._materialize(
            output,
            pdf,
            "https://download",
            session=_TlsFailureSession(),
            data_id="rw-test",
            relative_pdf_path="paper.pdf",
            model_version="vlm",
        )
    assert "CA bundle" in str(error.value)
    assert not output.exists()
    assert not list(tmp_path.glob(f".{output.name}.*"))


def test_missing_mineru_token_is_zero_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = _module()
    pdf_dir = tmp_path / "authorized"
    pdf_dir.mkdir()
    pdf = pdf_dir / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    output = tmp_path / "parser-output"
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    monkeypatch.setattr(module, "_token_file_candidates", lambda: ())
    with pytest.raises(module.MinerUAdapterError, match="MINERU_TOKEN_UNAVAILABLE"):
        module.main(["--pdf", str(pdf), "--input-dir", str(pdf_dir), "--output-dir", str(output)])
    assert not output.exists()
