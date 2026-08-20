"""Shared, attributable verification decisions bound to immutable objects."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas/project/verification_decision.v1.schema.json"
)


class VerificationDecisionError(ValueError):
    """A stable verification-decision contract failure."""


def verification_decision(
    *,
    actor_type: str,
    actor_label: str,
    action: str,
    reason: str,
    bound_object_digest: str,
    bound_gate_digest: str | None = None,
    decided_at: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "verification-decision.v1",
        "actor_type": actor_type,
        "actor_label": actor_label.strip() if isinstance(actor_label, str) else actor_label,
        "action": action,
        "reason": reason.strip() if isinstance(reason, str) else reason,
        "decided_at": decided_at or datetime.now(UTC).isoformat(),
        "bound_object_digest": bound_object_digest,
    }
    if bound_gate_digest is not None:
        payload["bound_gate_digest"] = bound_gate_digest
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VerificationDecisionError("VERIFICATION_DECISION_SCHEMA_INVALID") from exc
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    if errors:
        raise VerificationDecisionError("VERIFICATION_DECISION_INVALID")
    return payload
