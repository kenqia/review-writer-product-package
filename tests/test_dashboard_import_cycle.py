"""Regression coverage for the dashboard/public-agent import order."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_dashboard_module_imports_without_public_entry_cycle() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "from view import serve_review_dashboard"],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
