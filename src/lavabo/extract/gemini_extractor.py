"""Gemini extractor.

Same contract as the Claude one, using response_schema + JSON mime type for structured
output. Swap providers with a single line in config.yaml; the extraction cache is keyed by
model, so results from the two never mix.
"""

from __future__ import annotations

import json
import logging
import time

from ..models import Conversation, ExtractionResult
from .base import Extractor
from .prompt import PROMPT_VERSION, SYSTEM, build_user_prompt

log = logging.getLogger(__name__)

MAX_RETRIES = 5
RETRY_BASE_SECONDS = 20     # free-tier quotas are per-minute, so waits need to be long


def _is_rate_limit(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in ("429", "resource_exhausted", "resourceexhausted",
                                   "rate limit", "quota"))


def _is_unknown_model(exc: Exception) -> bool:
    text = str(exc).lower()
    return ("not found" in text or "not_found" in text or "404" in text
            or "is not supported" in text or "unknown model" in text) and "model" in text


def _to_gemini_schema(node: dict) -> dict:
    """Translate the JSON Schema in config.py to Gemini's dialect.

    Gemini rejects union types like ["string", "null"]; nullability is a separate flag.
    """
    if not isinstance(node, dict):
        return node

    out: dict = {}
    node_type = node.get("type")
    if isinstance(node_type, list):
        non_null = [t for t in node_type if t != "null"]
        out["type"] = (non_null[0] if non_null else "string").upper()
        out["nullable"] = "null" in node_type
    elif isinstance(node_type, str):
        out["type"] = node_type.upper()

    if desc := node.get("description"):
        out["description"] = desc
    if enum := node.get("enum"):
        out["enum"] = [e for e in enum if e is not None]
        out["nullable"] = True
    if props := node.get("properties"):
        out["properties"] = {k: _to_gemini_schema(v) for k, v in props.items()}
    if items := node.get("items"):
        out["items"] = _to_gemini_schema(items)
    if required := node.get("required"):
        out["required"] = required
    return out


class GeminiExtractor(Extractor):
    API_KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    @classmethod
    def verify_api_key(cls) -> tuple[bool, str]:
        if problem := cls.key_problem():
            return False, problem
        try:
            from google import genai
        except ImportError:
            return False, "google-genai not installed (pip install google-genai)"
        try:
            next(iter(genai.Client().models.list()), None)
        except Exception as exc:
            # Google reports a bad key as 400 INVALID_ARGUMENT / "API key not valid",
            # not 401, so matching only on 401 would misreport it as a network problem.
            text = str(exc).lower()
            rejected = any(m in text for m in (
                "api key not valid", "api_key_invalid", "invalid api key",
                "unauthenticated", "permission_denied", "401", "403",
            ))
            if rejected:
                return False, ("the provider rejected this key — check it was pasted in "
                               "full and belongs to a project with the API enabled")
            return True, (f"key set, but could not be verified "
                          f"({type(exc).__name__}) — network issue?")
        return True, "key verified with the provider"

    @classmethod
    def list_models(cls) -> list[str]:
        from google import genai

        names = []
        for m in genai.Client().models.list():
            actions = getattr(m, "supported_actions", None) or []
            if actions and "generateContent" not in actions:
                continue
            name = (getattr(m, "name", "") or "").removeprefix("models/")
            if name:
                names.append(name)
        return sorted(names)

    def __init__(self, config, schema) -> None:
        super().__init__(config, schema)
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError("pip install google-genai") from exc
        self.require_api_key()
        self.genai = genai
        self.client = genai.Client()  # reads GEMINI_API_KEY / GOOGLE_API_KEY

    def extract(self, conv: Conversation) -> ExtractionResult:
        result = ExtractionResult(
            conversation_id=conv.conversation_id,
            source=conv.source,
            values=self._empty_values(),
            model=self.model,
            schema_version=self.schema.version,
            schema_hash=self.schema.fingerprint(),
            prompt_version=PROMPT_VERSION,
        )

        request = dict(
            model=self.model,
            contents=build_user_prompt(
                conv, self.schema,
                max_chars=self.config.max_transcript_chars,
                display_timezone=self.config.display_timezone,
            ),
            config={
                "system_instruction": SYSTEM,
                "temperature": self.config.temperature,
                "max_output_tokens": self.config.max_tokens,
                "response_mime_type": "application/json",
                "response_schema": _to_gemini_schema(self.schema.json_schema()),
            },
        )

        try:
            response = self._generate(request)
        except Exception as exc:
            log.error("extraction failed for %s: %s", conv.conversation_id, exc)
            result.error = f"{type(exc).__name__}: {exc}"
            if _is_unknown_model(exc):
                result.error += (f"  [model {self.model!r} was not accepted — "
                                 "run `lavabo models` to see what this key can use]")
            return result

        if usage := getattr(response, "usage_metadata", None):
            result.input_tokens = getattr(usage, "prompt_token_count", 0) or 0
            result.output_tokens = getattr(usage, "candidates_token_count", 0) or 0

        try:
            result.values = self._coerce(json.loads(response.text))
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            result.error = f"unparseable response: {exc}"
            log.error("%s: %s", conv.conversation_id, result.error)

        return result

    def _generate(self, request: dict):
        """One call, waiting out rate limits. Raises on anything it cannot retry.

        The free tier allows only a handful of requests per minute, so a 429 is an
        expected part of normal operation rather than a failure.
        """
        for attempt in range(MAX_RETRIES):
            try:
                return self.client.models.generate_content(**request)
            except Exception as exc:
                if _is_rate_limit(exc) and attempt < MAX_RETRIES - 1:
                    wait = RETRY_BASE_SECONDS * (2 ** attempt)
                    log.warning("rate limited (free tier?), retrying in %ss [%d/%d]",
                                wait, attempt + 1, MAX_RETRIES - 1)
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError("no response after retries")

    def complete_json(self, system: str, user: str, schema: dict,
                      *, max_tokens: int = 0):
        """Structured JSON from one prompt. Used by segmentation, which is not an
        extraction: its own prompt, its own schema, no Conversation.

        max_tokens is passed per call rather than taken from config, because the config
        value is sized for one order's fields while a segmentation answer covers a whole
        month of them. Sharing it truncated the first live run.
        """
        from ..segment import Completion

        response = self._generate(dict(
            model=self.model,
            contents=user,
            config={
                "system_instruction": system,
                "temperature": self.config.temperature,
                "max_output_tokens": max_tokens or self.config.max_tokens,
                "response_mime_type": "application/json",
                "response_schema": _to_gemini_schema(schema),
            },
        ))
        usage = getattr(response, "usage_metadata", None)
        candidates = getattr(response, "candidates", None) or []
        finish = getattr(candidates[0], "finish_reason", "") if candidates else ""
        return Completion(
            json.loads(response.text),
            getattr(usage, "prompt_token_count", 0) or 0,
            getattr(usage, "candidates_token_count", 0) or 0,
            str(getattr(finish, "name", finish) or ""),
        )
