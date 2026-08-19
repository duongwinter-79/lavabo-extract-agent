"""Extractor contract + provider factory."""

from __future__ import annotations

import logging
import os
import random
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TypeVar

from ..config import ExtractConfig, ExtractionSchema
from ..models import Conversation, ExtractionResult

log = logging.getLogger(__name__)
T = TypeVar("T")

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


# --------------------------------------------------------------------------- retrying

# Two kinds of "try again", with very different right answers.
#
# A quota error is a promise about the next SIXTY SECONDS: the free tier allows a handful
# of requests per minute, so retrying sooner than that just spends another refusal. A 503
# is the opposite -- the provider is momentarily overloaded and usually recovers in
# seconds, so waiting twenty of them for every attempt turns a blip into a minute of
# nothing.
#
# Getting this wrong was silent. 503 matched none of the quota patterns, so an overloaded
# provider failed the call outright: during extraction that leaves an order's AI columns
# blank, and during segmentation it drops the whole paste to the regex fallback -- losing
# exactly the revisions the model is there to find.
RATE_LIMIT_MARKERS = ("429", "resource_exhausted", "resourceexhausted", "rate limit",
                      "rate_limit", "quota", "too many requests")
TRANSIENT_MARKERS = ("503", "502", "504", "500", "unavailable", "overloaded",
                     "internal error", "internal server", "bad gateway", "timeout",
                     "timed out", "connection reset", "connection aborted",
                     "connection error", "remote disconnected", "temporarily")

RATE_LIMIT_BASE_SECONDS = 20        # free-tier quotas are per-minute
TRANSIENT_BASE_SECONDS = 2          # overload clears in seconds
MAX_ATTEMPTS = 5


def is_rate_limit(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in RATE_LIMIT_MARKERS)


def is_transient(exc: BaseException) -> bool:
    """A provider-side hiccup worth trying again, as opposed to a request that is wrong.

    Checked AFTER is_rate_limit, since a 429 mentioning "quota" is both by these patterns
    and the longer wait is the correct one.
    """
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in TRANSIENT_MARKERS)


def retry_call(call: Callable[[], T], *, what: str = "request",
               deadline_seconds: float | None = None,
               sleep: Callable[[float], None] = time.sleep,
               monotonic: Callable[[], float] = time.monotonic) -> T:
    """Run `call`, waiting out rate limits and provider hiccups. Raises anything else.

    `deadline_seconds` bounds the whole thing, because these calls are not all equal.
    Extraction runs in a batch nobody is watching and can afford minutes. Segmentation
    happens while somebody is holding a phone waiting for "Lưu đơn" to come back, and a
    five-minute spin there is worse than falling back to the regexes and saying so -- the
    paste is on disk either way, so a fallback can be re-run later at no cost.

    A wait that would cross the deadline is not taken: sleeping past it and then trying
    anyway spends the time without respecting the limit.

    `sleep` and `monotonic` are injectable together so the deadline can be tested without
    a test that actually waits minutes -- and so a fake sleep advances the fake clock,
    which a test using only a fake sleep would not, quietly proving nothing.
    """
    started = monotonic()
    last: BaseException | None = None

    for attempt in range(MAX_ATTEMPTS):
        try:
            return call()
        except Exception as exc:
            last = exc
            rate_limited = is_rate_limit(exc)
            if not rate_limited and not is_transient(exc):
                raise
            if attempt == MAX_ATTEMPTS - 1:
                raise

            base = RATE_LIMIT_BASE_SECONDS if rate_limited else TRANSIENT_BASE_SECONDS
            wait = base * (2 ** attempt)
            # Jitter so several workers that hit the same outage do not all come back at
            # the same instant and cause the next one.
            wait += random.uniform(0, wait * 0.1)
            if deadline_seconds is not None:
                left = deadline_seconds - (monotonic() - started)
                if wait > left:
                    log.warning("%s: %s — giving up, retrying would exceed the %ss budget",
                                what, exc, deadline_seconds)
                    raise
            log.warning("%s: %s — retrying in %.0fs [%d/%d]",
                        what, "rate limited" if rate_limited else "provider unavailable",
                        wait, attempt + 1, MAX_ATTEMPTS - 1)
            sleep(wait)

    raise last if last else RuntimeError(f"{what}: no attempt was made")
