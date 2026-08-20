"""Standard-library-only portable source-path validation for M0."""
from __future__ import annotations
import re
import stat
from pathlib import Path

class PathSafetyError(ValueError): pass

def _is_reparse_component(path: Path) -> bool:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()): return True
    try: return bool(getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except OSError: return False

def validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value) or value.startswith("//"):
        raise PathSafetyError("noncanonical or absolute path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts): raise PathSafetyError("dot/traversal path")
    return "/".join(parts)

def validate_source_file(root: Path, relative: str) -> Path:
    relative = validate_relative_path(relative)
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise PathSafetyError("source root does not exist or cannot be resolved") from exc
    candidate = root
    for part in relative.split("/"):
        candidate = candidate / part
        if _is_reparse_component(candidate): raise PathSafetyError("source input component is reparse point")
    try:
        actual = candidate.resolve(strict=True)
    except OSError as exc:
        raise PathSafetyError("source input does not exist or cannot be resolved") from exc
    try: actual.relative_to(root)
    except ValueError as exc: raise PathSafetyError("reparse escape") from exc
    if not actual.is_file(): raise PathSafetyError("not ordinary file")
    return actual

def validate_source_inputs(root: Path, relatives: list[str]) -> list[Path]:
    """Validate the exact multi-input collision rule used by ProjectManifest."""
    seen: set[str] = set(); files: list[Path] = []
    for relative in relatives:
        canonical = validate_relative_path(relative); key = canonical.casefold()
        if key in seen: raise PathSafetyError("cross-platform path collision")
        seen.add(key); files.append(validate_source_file(root, canonical))
    return files
