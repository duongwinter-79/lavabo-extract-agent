"""Extractor contract + provider factory."""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod

from ..config import ExtractConfig, ExtractionSchema
from ..models import Conversation, ExtractionResult

# Words that only appear in a template, never in a key copied from a provider console.
PLACEHOLDER_WORDS = re.compile(
    r"\b(?:your|changeme|placeholder|dummy|example|paste)[-_ ]?(?:api[-_ ]?)?key\b",
    re.IGNORECASE,
)

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
    def api_key(cls) -> str:
        for var in cls.API_KEY_VARS:
            if value := (os.environ.get(var) or "").strip():
                return value
        return ""

    @classmethod
    def key_problem(cls) -> str | None:
        """Describe what is wrong with the configured key, or None if it looks usable.

        Catches the case that actually bites: `.env` copied from `.env.example` with
        its placeholder left in. A non-empty variable is not evidence of a real key,
        so checking only for emptiness reports success and then 401s at extraction.
        """
        if not cls.API_KEY_VARS:
            return None

        key = cls.api_key()
        names = " or ".join(cls.API_KEY_VARS)
        if not key:
            return f"{names} is not set"

        # Deliberately narrow. Real keys are long random strings, so a loose substring
        # test ("xxx") rejects valid keys that happen to contain those letters. Only
        # shapes a human would never paste from a key page count as placeholders.
        placeholder = (
            "..." in key
            or "<" in key
            or ">" in key
            or key.endswith("-")
            or PLACEHOLDER_WORDS.search(key) is not None
        )
        if placeholder:
            return (f"{names} still looks like the placeholder from .env.example "
                    f"({key[:12]}…) — paste your real key")
        if len(key) < 30:
            return (f"{names} is only {len(key)} characters, which is too short for a real "
                    "key — it may have been truncated when pasted")
        return None

    @classmethod
    def require_api_key(cls) -> None:
        """Fail early with a readable message instead of an SDK error per conversation."""
        if problem := cls.key_problem():
            raise RuntimeError(
                f"{problem}. Put it in .env (see .env.example) or export it. "
                "Tip: `lavabo extract --dry-run` works without a key."
            )

    @classmethod
    def has_api_key(cls) -> bool:
        return cls.key_problem() is None

    @classmethod
    def verify_api_key(cls) -> tuple[bool, str]:
        """Ask the provider whether the key actually works. (ok, message).

        Worth a network round trip: an invalid key is otherwise only discovered after
        a capture session, at the first billed call.
        """
        if problem := cls.key_problem():
            return False, problem
        return True, "key present (not verified against the provider)"

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
