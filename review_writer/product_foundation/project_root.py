"""Explicit project-root resolution for the product-foundation kernel."""

from __future__ import annotations

from pathlib import Path

from .contracts import InvalidContextError


def resolve_project_root(explicit_project_root: str | Path) -> Path:
    """Return the normalized project root supplied explicitly by the caller."""

    if explicit_project_root is None:
        raise InvalidContextError("project root must be explicit")
    if isinstance(explicit_project_root, str) and not explicit_project_root.strip():
        raise InvalidContextError("project root must not be blank")

    try:
        supplied = Path(explicit_project_root)
    except (TypeError, ValueError) as exc:
        raise InvalidContextError("project root is invalid") from exc

    if not supplied.is_absolute():
        raise InvalidContextError("project root must be absolute")

    try:
        resolved = supplied.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InvalidContextError("project root is invalid") from exc
    if not resolved.is_dir():
        raise InvalidContextError("project root must be a directory")
    return resolved


def version_context_root(explicit_project_root: str | Path) -> Path:
    """Return the deterministic version-context directory without creating it."""

    return resolve_project_root(explicit_project_root) / ".review-writer" / "version_context"
