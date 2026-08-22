"""Focused static contract checks for the Windows/QoderWork setup slice."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "scripts/windows"
INSTALLER = WINDOWS / "Install-ReviewWriter.ps1"
ENV_TEST = WINDOWS / "Test-ReviewWriterEnvironment.ps1"
ENV_EXAMPLE = ROOT / ".env.example"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_windows_setup_files_and_example_are_present() -> None:
    assert INSTALLER.is_file()
    assert ENV_TEST.is_file()
    assert ENV_EXAMPLE.is_file()


def test_env_example_documents_only_the_optional_parser_path() -> None:
    text = _read(ENV_EXAMPLE)
    assignments = [line for line in text.splitlines() if line and not line.lstrip().startswith("#")]
    assert assignments == ["REVIEW_WRITER_MINERU_PARSER="]
    assert "DASHSCOPE" not in text
    assert "API_KEY=" not in text
    assert not re.search(r"(?i)(sk-[A-Za-z0-9_-]{12,}|AKIA[0-9A-Z]{12,})", text)


def test_installer_is_offline_fail_closed_and_runs_environment_test() -> None:
    text = _read(INSTALLER)
    assert "Set-StrictMode -Version Latest" in text
    assert '$ErrorActionPreference = "Stop"' in text
    assert "-m venv" in text
    assert "-m pip install -r" in text
    assert "Test-ReviewWriterEnvironment.ps1" in text
    assert "build_qoderwork_plugin_zip.py" in text
    assert "Invoke-WebRequest" not in text
    assert "Invoke-RestMethod" not in text
    assert "Get-ChildItem Env:" not in text
    assert "DASHSCOPE" not in text
    assert "API_KEY=" not in text
    assert re.search(r"\$env:REVIEW_WRITER_MINERU_PARSER", text) is None


def test_environment_test_checks_only_the_documented_parser_variable() -> None:
    text = _read(ENV_TEST)
    assert "Set-StrictMode -Version Latest" in text
    assert '$ErrorActionPreference = "Stop"' in text
    assert "Python 3.11" in text
    assert "pdftotext" in text
    assert "REVIEW_WRITER_MINERU_PARSER" in text
    assert "Get-ChildItem Env:" not in text
    assert "Get-Item Env:" not in text
    assert "DASHSCOPE" not in text
    assert "API_KEY=" not in text
    assert "DASHSCOPE_API_KEY" not in text
    assert "COOKIE=" not in text
    assert "SESSION=" not in text
    assert re.search(r"Write-(Host|Output).*REVIEW_WRITER_MINERU_PARSER[^\r\n]*\$env:", text) is None


def test_windows_docs_explain_native_install_and_human_gate_boundary() -> None:
    readme = _read(ROOT / "README.md")
    qoderwork = _read(ROOT / "docs-qoderwork-cn.md")
    assert "Install-ReviewWriter.ps1" in readme
    assert "Test-ReviewWriterEnvironment.ps1" in readme
    assert ".env.example" in readme
    assert "Windows" in readme
    assert "HUMAN_ACTION_REQUIRED" in readme
    assert "Install-ReviewWriter.ps1" in qoderwork
    assert "REVIEW_WRITER_MINERU_PARSER" in qoderwork
    assert "QoderWork CN" in qoderwork


def test_cr011_records_exact_windows_write_set_and_holds_product_acceptance() -> None:
    manifest = _read(ROOT / "product-package-manifest.md")
    traceability = _read(ROOT / "docs/PRODUCT_TRACEABILITY.md")
    notices = _read(ROOT / "docs/THIRD_PARTY_NOTICES.md")
    for text in (manifest, traceability, notices):
        assert "CR-011" in text
        assert "Install-ReviewWriter.ps1" in text
        assert "Test-ReviewWriterEnvironment.ps1" in text
        assert "REVIEW_WRITER_MINERU_PARSER" in text
    assert "HUMAN_ACCEPTANCE" in traceability
    assert "HOLD" in traceability
    assert "no secret" in notices.lower() or "secret" in notices.lower()
