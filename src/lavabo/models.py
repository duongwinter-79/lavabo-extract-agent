"""Canonical data model. Every connector must produce these types and nothing else."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from enum import Enum
from typing import Any


class Source(str, Enum):
    ZALO = "zalo"
    MESSENGER = "messenger"
    INSTAGRAM = "instagram"


class Direction(str, Enum):
    INBOUND = "inbound"      # customer -> us
    OUTBOUND = "outbound"    # us -> customer
    SYSTEM = "system"        # joins, renames, unsupported events


@dataclass(slots=True)
class Attachment:
    kind: str                       # image | file | video | audio | sticker | share
    name: str | None = None
    url: str | None = None


@dataclass(slots=True)
class Message:
    source: Source
    conversation_id: str
    message_id: str
    sent_at: datetime               # MUST be tz-aware UTC
    direction: Direction
    text: str = ""
    sender_id: str | None = None
    sender_name: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.sent_at.tzinfo is None:
            raise ValueError(
                f"{self.source}/{self.message_id}: sent_at must be tz-aware. "
                "Normalize in the connector, not downstream."
            )


@dataclass(slots=True)
class Conversation:
    source: Source
    conversation_id: str
    messages: list[Message] = field(default_factory=list)
    customer_name: str | None = None
    customer_handle: str | None = None
    origin: str | None = None       # thread permalink, or source filename for Zalo
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def started_at(self) -> datetime | None:
        return self.messages[0].sent_at if self.messages else None

    @property
    def last_message_at(self) -> datetime | None:
        return self.messages[-1].sent_at if self.messages else None

    def sort(self) -> None:
        """Chronological order. Connectors returning newest-first must call this."""
        self.messages.sort(key=lambda m: (m.sent_at, m.message_id))

    def transcript(self, *, include_timestamps: bool = True, tz: tzinfo | None = None) -> str:
        """Flatten to the plain text handed to the LLM.

        Storage is always UTC, but the transcript is rendered in `tz` (the business's
        local time) so the model reports times that match what a human sees in the app.
        """
        lines = []
        for m in self.messages:
            who = m.sender_name or ("Customer" if m.direction is Direction.INBOUND else "Agent")
            local = m.sent_at.astimezone(tz) if tz else m.sent_at
            stamp = f"[{local:%Y-%m-%d %H:%M}] " if include_timestamps else ""
            body = m.text.strip()
            if m.attachments:
                tags = ", ".join(a.kind for a in m.attachments)
                body = f"{body} <attachment: {tags}>".strip()
            if body:
                lines.append(f"{stamp}{who}: {body}")
        return "\n".join(lines)

    def content_hash(self) -> str:
        """Stable fingerprint of conversation content, for extraction caching."""
        payload = json.dumps(
            [[m.message_id, m.sent_at.isoformat(), m.direction.value, m.text] for m in self.messages],
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ExtractionResult:
    conversation_id: str
    source: Source
    values: dict[str, Any]                      # column name -> extracted value
    confidence: dict[str, float] = field(default_factory=dict)
    model: str = ""
    schema_version: int = 0
    prompt_version: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
