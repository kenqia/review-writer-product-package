"""Focused static contract checks for CR-018 QoderWork research packets."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "qoderwork/plugins/review-writer-cn"
SKILL = PLUGIN / "skills/review-writer/SKILL.md"
HOST = PLUGIN / "qoderwork.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_qoderwork_consumes_planned_research_packet_after_public_next_action() -> None:
    """The host must follow the public plan instead of exploring implementation details."""

    skill = _read(SKILL)
    host = _read(HOST)
    contract = f"{skill}\n{host}"

    for required in (
        "next_action",
        "research packet",
        "canonical source-bound",
        "parse provenance",
        "page",
        "section",
        "quote",
        "digest",
        "Evidence candidates",
        "comparison",
        "synthesis",
        "section",
        "figure",
        "GAP",
        "rights",
    ):
        assert required.lower() in contract.lower()


def test_qoderwork_is_public_surface_only_and_stops_at_human_action() -> None:
    """No internal exploration, state mutation, or unsourced scientific output is allowed."""

    skill = _read(SKILL)
    host = _read(HOST)
    contract = f"{skill}\n{host}"

    for required in (
        "topic",
        "explicit project root",
        "authorized PDF folder",
        "public Agent",
        "public Skill",
        "Dashboard",
        "HUMAN_ACTION_REQUIRED",
        "stop",
        "do not scan the repository",
        "do not search for internal workflow",
        "CLI",
        "curl",
        "pytest",
        "generator",
        "internal JSON",
        "VersionContext",
        "unsourced claim",
        "unsourced figure",
    ):
        assert required.lower() in contract.lower()
