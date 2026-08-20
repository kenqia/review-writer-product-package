"""Bounded, manifest-driven source acquisition."""

from .manual_archive import ManualArchiveError, import_manual_archive
from .public_corpus import ManifestError, acquire_manifest

__all__ = ["ManifestError", "ManualArchiveError", "acquire_manifest", "import_manual_archive"]
