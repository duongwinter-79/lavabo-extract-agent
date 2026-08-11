"""Extractor contract + provider factory."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import ExtractConfig, ExtractionSchema
from ..models import Conversation, ExtractionResult

TOOL_NAME = "record_extraction"
TOOL_DESCRIPTION = (
    "Record the structured data extracted from this conversation. "
    "Every field must be present; use null for anything the conversation does not state."
)


class Extractor(ABC):
    model: str

    def __init__(self, config: ExtractConfig, schema: ExtractionSchema) -> None:
        self.config = config
        self.schema = schema
        self.model = config.model

    @abstractmethod
    def extract(self, conv: Conversation) -> ExtractionResult:
        """Run one conversation through the model and return validated values."""

    def _empty_values(self) -> dict[str, None]:
        return {c.name: None for c in self.schema.columns}

    def _coerce(self, raw: dict) -> dict:
        """Force the model's output onto exactly the declared columns.

        Extra keys are dropped and missing keys become null, so a schema drift in the
        model's response can never corrupt the Excel layout.
        """
        return {c.name: raw.get(c.name) for c in self.schema.columns}


def build_extractor(config: ExtractConfig, schema: ExtractionSchema) -> Extractor:
    provider = config.provider.lower()
    if provider == "anthropic":
        from .anthropic_extractor import AnthropicExtractor
        return AnthropicExtractor(config, schema)
    if provider == "gemini":
        from .gemini_extractor import GeminiExtractor
        return GeminiExtractor(config, schema)
    raise ValueError(f"unknown extract.provider {config.provider!r} (expected 'anthropic' or 'gemini')")
