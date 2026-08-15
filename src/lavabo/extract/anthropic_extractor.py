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


class AnthropicExtractor(Extractor):
    API_KEY_VARS = ("ANTHROPIC_API_KEY",)

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
            prompt_version=PROMPT_VERSION,
        )

        try:
            response = self.client.messages.create(
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
            )
        except Exception as exc:
            log.error("extraction failed for %s: %s", conv.conversation_id, exc)
            result.error = f"{type(exc).__name__}: {exc}"
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
