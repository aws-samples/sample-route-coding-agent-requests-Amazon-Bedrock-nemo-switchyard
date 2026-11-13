"""Gateway glue between LiteLLM and the Switchyard routing plugin."""

from .plugin import MultiCandidateRouting

__all__ = ["MultiCandidateRouting"]
