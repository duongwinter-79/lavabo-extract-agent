"""Claude extractor.

Uses a forced tool call rather than asking for JSON in prose: the tool's input_schema is
generated from config/schema.yaml, and tool_choice pins that tool, so the model physically
cannot return something unparseable.
"""

from __future__ import annotations

import logging

from ..models import Conversation, ExtractionResult
from .base import TOOL_DESCRIPTION, TOOL_NAME, Extractor
from .prompt import PROMPT_VERSION, SYSTEM, build_user_prompt

log = logging.getLogger(__name__)

# Segmentation runs while somebody waits for "Lưu đơn" to come back; extraction runs in a
# batch nobody is watching. See base.retry_call.
SEGMENT_DEADLINE_SECONDS = 90

# A batch job on a laptop, not something anyone waits on. See the Gemini one.
VIDEO_DEADLINE_SECONDS = 600


class AnthropicExtractor(Extractor):
    API_KEY_VARS = ("ANTHROPIC_API_KEY",)

    @classmethod
    def verify_api_key(cls) -> tuple[bool, str]:
        """Confirm the key works, using models.list() -- no tokens, no charge."""
        if problem := cls.key_problem():
            return False, problem
        try:
            import anthropic
        except ImportError:
            return False, "anthropic package not installed (pip install anthropic)"

        try:
            anthropic.Anthropic(timeout=15.0).models.list(limit=1)
        except Exception as exc:
            name = type(exc).__name__
            if "Authentication" in name or "401" in str(exc):
                return False, ("the provider rejected this key (401). It is set but not "
                               "valid — check for a stray space, a truncated paste, or a "
                               "key from a different account")
            return True, f"key set, but could not be verified ({name}) — network issue?"
        return True, "key verified with the provider"

    @classmethod
    def list_models(cls) -> list[str]:
        import anthropic

        return sorted(m.id for m in anthropic.Anthropic().models.list(limit=100))

    def __init__(self, config, schema) -> None:
        super().__init__(config, schema)
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError("pip install anthropic") from exc
        self.require_api_key()
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

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

        # Retried on rate limits and provider hiccups alike. This had no retry at all:
        # a single 529/503 lost the order's AI columns, and on the segmentation path it
        # dropped a whole paste to the regex fallback.
        try:
            response = retry_call(
                lambda: self.client.messages.create(
                    model=self.model,
                    max_tokens=self.config.max_tokens,
                    temperature=self.config.temperature,
                    system=SYSTEM,
                    tools=[{
                        "name": TOOL_NAME,
                        "description": TOOL_DESCRIPTION,
                        "input_schema": self.schema.json_schema(),
                    }],
                    tool_choice={"type": "tool", "name": TOOL_NAME},
                    messages=[{
                        "role": "user",
                        "content": build_user_prompt(
                            conv, self.schema,
                            max_chars=self.config.max_transcript_chars,
                            display_timezone=self.config.display_timezone,
                        ),
                    }],
                ),
                what=f"extract {conv.conversation_id}")
        except Exception as exc:
            log.error("extraction failed for %s: %s", conv.conversation_id, exc)
            result.error = f"{type(exc).__name__}: {exc}"
            if "not_found" in str(exc).lower() or "404" in str(exc):
                result.error += (f"  [model {self.model!r} was not accepted — "
                                 "run `lavabo models` to see what this key can use]")
            return result

        result.input_tokens = response.usage.input_tokens
        result.output_tokens = response.usage.output_tokens

        for block in response.content:
            if block.type == "tool_use" and block.name == TOOL_NAME:
                result.values = self._coerce(block.input)
                return result

        result.error = f"model returned no {TOOL_NAME} tool call (stop_reason={response.stop_reason})"
        log.error("%s: %s", conv.conversation_id, result.error)
        return result

    def complete_json(self, system: str, user: str, schema: dict,
                      *, max_tokens: int = 0):
        """Structured JSON from one prompt, via a forced tool call.

        Used by segmentation, which is not an extraction: its own prompt, its own schema,
        no Conversation. Forcing the tool is what makes the shape guaranteed rather than
        merely likely, which matters when the caller is deciding how many orders exist.
        """
        from ..segment import Completion

        tool_name = "record_segmentation"
        response = retry_call(
            lambda: self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens or self.config.max_tokens,
                temperature=self.config.temperature,
                system=system,
                tools=[{
                    "name": tool_name,
                    "description": "Record the orders found in this chat.",
                    "input_schema": schema,
                }],
                tool_choice={"type": "tool", "name": tool_name},
                messages=[{"role": "user", "content": user}],
            ),
            what="segmentation", deadline_seconds=SEGMENT_DEADLINE_SECONDS)
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                return Completion(block.input,
                                  response.usage.input_tokens,
                                  response.usage.output_tokens,
                                  # Named to match Gemini's, so the caller's truncation
                                  # check does not need to know which provider answered.
                                  "MAX_TOKENS" if response.stop_reason == "max_tokens"
                                  else str(response.stop_reason or ""))
        raise RuntimeError(
            f"model returned no {tool_name} tool call (stop_reason={response.stop_reason})")

    def complete_json_images(self, system: str, user: str, images: list[bytes],
                             schema: dict, *, max_tokens: int = 0,
                             mime_type: str = "image/png"):
        """Structured JSON from a prompt plus a series of images, via a forced tool call."""
        import base64

        from ..segment import Completion

        tool_name = "record_segmentation"
        content: list[dict] = [{"type": "text", "text": user}]
        content += [{"type": "image",
                     "source": {"type": "base64", "media_type": mime_type,
                                "data": base64.b64encode(image).decode("ascii")}}
                    for image in images]
        response = retry_call(
            lambda: self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens or self.config.max_tokens,
                temperature=self.config.temperature,
                system=system,
                tools=[{"name": tool_name,
                        "description": "Record the orders visible in these frames.",
                        "input_schema": schema}],
                tool_choice={"type": "tool", "name": tool_name},
                messages=[{"role": "user", "content": content}],
            ),
            what="video segmentation", deadline_seconds=VIDEO_DEADLINE_SECONDS)
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                return Completion(block.input,
                                  response.usage.input_tokens,
                                  response.usage.output_tokens,
                                  "MAX_TOKENS" if response.stop_reason == "max_tokens"
                                  else str(response.stop_reason or ""))
        raise RuntimeError(
            f"model returned no {tool_name} tool call (stop_reason={response.stop_reason})")
