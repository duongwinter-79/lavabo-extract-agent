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

# Prefixes that unambiguously identify a provider. Used only to catch a model
# configured for the wrong one; unknown prefixes are left alone.
MODEL_OWNERS = {
    "anthropic": ("claude",),
    "gemini": ("gemini", "gemma", "models/gemini"),
}


def _provider_for_model(model: str) -> str | None:
    name = (model or "").strip().lower()
    for provider, prefixes in MODEL_OWNERS.items():
        if any(name.startswith(p) for p in prefixes):
            return provider
    return None


TOOL_NAME = "record_extraction"
TOOL_DESCRIPTION = (
    "Record the structured data extracted from this conversation. "
    "Every field must be present; use null for anything the conversation does not state."
)


class Extractor(ABC):
    model: str
    # Env var(s) that can supply this provider's key; any one is enough.
    API_KEY_VARS: tuple[str, ...] = ()
    # Model-name prefixes that belong to this provider.
    MODEL_PREFIXES: tuple[str, ...] = ()

    def __init__(self, config: ExtractConfig, schema: ExtractionSchema) -> None:
        self.config = config
        self.schema = schema
        self.model = config.model
        self.check_model_matches_provider(config.provider, config.model)

    @staticmethod
    def check_model_matches_provider(provider: str, model: str) -> None:
        """Reject a model that plainly belongs to a different provider.

        provider and model are separate settings, so switching one leaves the other
        behind -- a Gemini provider still pointing at "claude-opus-5" only fails at
        the API, as a 404 that reads like a missing model rather than a mismatch.

        Deliberately only fires on a KNOWN other-provider prefix: unfamiliar names
        must still work, since model line-ups change faster than this code.
        """
        owner = _provider_for_model(model)
        if owner and owner != provider.lower():
            raise RuntimeError(
                f"extract.provider is {provider!r} but extract.model is {model!r}, "
                f"which is a {owner} model. Set extract.model in config/config.yaml to a "
                f"{provider} model (run `lavabo models` to list them), or pass "
                f"--model. Note config/config.yaml is yours and is never "
                f"updated by git pull -- config.example.yaml changing does not change it."
            )

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

    @classmethod
    def list_models(cls) -> list[str]:
        """Model ids this key can actually use.

        Model line-ups move faster than any hardcoded list, so the authority is the
        provider, not a constant in this repo or a guess from either end.
        """
        raise NotImplementedError(f"{cls.__name__} cannot list models")

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
