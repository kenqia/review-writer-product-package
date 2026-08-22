"""Shared public Agent wrapper for FR-027 E2E tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from review_writer.agent import start_or_resume_review


def run_agent(topic: str, project_root: Path, authorized_pdf_folder: Path) -> dict[str, Any]:
    """Expose only ordinary-user inputs to the canonical public entry."""
    return start_or_resume_review(topic, project_root, authorized_pdf_folder)
