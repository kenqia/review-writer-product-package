"""Focused tests for portable discovery of the optional MinerU parser."""

from __future__ import annotations

from pathlib import Path

from review_writer.agent import local_pdf_parse


def test_mineru_parser_resolution_honors_explicit_path(monkeypatch, tmp_path: Path) -> None:
    parser = tmp_path / "parse_review_writer_pdfs.py"
    parser.write_text("# test parser\n", encoding="utf-8")

    monkeypatch.setenv("REVIEW_WRITER_MINERU_PARSER", str(parser))
    monkeypatch.setattr(local_pdf_parse, "_MINERU_PARSER", None)

    assert local_pdf_parse._resolve_mineru_parser() == parser


def test_mineru_parser_resolution_uses_package_local_skill_before_home_or_path(
    monkeypatch, tmp_path: Path
) -> None:
    package_root = tmp_path / "package"
    parser = (
        package_root
        / ".agents/skills/mineru-precise-parse-review-writer/scripts/parse_review_writer_pdfs.py"
    )
    parser.parent.mkdir(parents=True)
    parser.write_text("# package-local parser\n", encoding="utf-8")

    monkeypatch.delenv("REVIEW_WRITER_MINERU_PARSER", raising=False)
    monkeypatch.setattr(local_pdf_parse, "_MINERU_PARSER", None)

    assert (
        local_pdf_parse._resolve_mineru_parser(
            package_root=package_root,
            home=tmp_path / "home",
            path_lookup=lambda _name: None,
        )
        == parser
    )


def test_mineru_parser_resolution_finds_user_skill_root(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    parser = (
        home
        / ".codex/skills/mineru-precise-parse-review-writer/scripts/parse_review_writer_pdfs.py"
    )
    parser.parent.mkdir(parents=True)
    parser.write_text("# user-skill parser\n", encoding="utf-8")

    monkeypatch.delenv("REVIEW_WRITER_MINERU_PARSER", raising=False)
    monkeypatch.setattr(local_pdf_parse, "_MINERU_PARSER", None)

    assert (
        local_pdf_parse._resolve_mineru_parser(
            package_root=tmp_path / "package",
            home=home,
            path_lookup=lambda _name: None,
        )
        == parser
    )


def test_mineru_parser_resolution_returns_none_for_clean_clone_without_backend(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("REVIEW_WRITER_MINERU_PARSER", raising=False)
    monkeypatch.setattr(local_pdf_parse, "_MINERU_PARSER", None)

    assert (
        local_pdf_parse._resolve_mineru_parser(
            package_root=tmp_path / "package",
            home=tmp_path / "home",
            path_lookup=lambda _name: None,
        )
        is None
    )
