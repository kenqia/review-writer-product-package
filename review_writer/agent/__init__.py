"""Public and internal product Agent runtimes."""

from .generator_runtime import GeneratorRuntimeError, GeneratorSession
from .decision_bundle import build_decision_bundle


# Public entry imports Dashboard helpers; defer it so Dashboard can import the
# internal generator runtime without executing the reverse dependency.
def __getattr__(name: str):
    if name == "PublicReviewEntryError":
        from .public_entry import PublicReviewEntryError

        return PublicReviewEntryError
    if name == "start_or_resume_review":
        from .public_entry import start_or_resume_review

        return start_or_resume_review
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "GeneratorRuntimeError",
    "GeneratorSession",
    "PublicReviewEntryError",
    "start_or_resume_review",
    "build_decision_bundle",
]
