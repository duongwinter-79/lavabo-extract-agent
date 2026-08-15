"""Extractor contract + provider factory."""

from __future__ import annotations

import os
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
    # Env var(s) that can supply this provider's key; any one is enough.
    API_KEY_VARS: tuple[str, ...] = ()

    def __init__(self, config: ExtractConfig, schema: ExtractionSchema) -> None:
        self.config = config
        self.schema = schema
        self.model = config.model

    @classmethod
    def require_api_key(cls) -> None:
        """Fail early with a readable message instead of an SDK error per conversation."""
        if not cls.API_KEY_VARS or any(os.environ.get(v) for v in cls.API_KEY_VARS):
            return
        names = " or ".join(cls.API_KEY_VARS)
        raise RuntimeError(
            f"{names} is not set. Put it in .env (see .env.example) or export it. "
            "Tip: `lavabo extract --dry-run` works without a key."
        )

    @classmethod
    def has_api_key(cls) -> bool:
        return bool(cls.API_KEY_VARS) and any(os.environ.get(v) for v in cls.API_KEY_VARS)

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


def extractor_class(provider: str) -> type[Extractor]:
    """Resolve a provider name to its class without constructing it (no key needed)."""
    match provider.lower():
        case "anthropic":
            from .anthropic_extractor import AnthropicExtractor
            return AnthropicExtractor
        case "gemini":
            from .gemini_extractor import GeminiExtractor
            return GeminiExtractor
    raise ValueError(f"unknown extract.provider {provider!r} (expected 'anthropic' or 'gemini')")


def build_extractor(config: ExtractConfig, schema: ExtractionSchema) -> Extractor:
    return extractor_class(config.provider)(config, schema)
