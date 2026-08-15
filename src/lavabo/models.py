"""Canonical data model. Every connector must produce these types and nothing else."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from enum import Enum
from typing import Any


class Source(str, Enum):
    ZALO = "zalo"                # copied out of the app by hand
    ZALO_OA = "zalo_oa"          # pushed to us by Zalo, from an OA-owned group
    MESSENGER = "messenger"
    INSTAGRAM = "instagram"


class Direction(str, Enum):
    INBOUND = "inbound"      # customer -> us
    OUTBOUND = "outbound"    # us -> customer
    SYSTEM = "system"        # joins, renames, unsupported events
    UNKNOWN = "unknown"      # source gave no speaker labels; inferred at extraction time


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
    # None when the source carries no timestamps at all -- a Zalo Web copy, for
    # instance, yields bare message text. Never invent one: a fabricated date is
    # worse than an admitted gap, because it silently poisons any date column.
    sent_at: datetime | None
    direction: Direction
    sequence: int = 0               # position in the conversation; orders untimed messages
    text: str = ""
    sender_id: str | None = None
    sender_name: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.sent_at is not None and self.sent_at.tzinfo is None:
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
    def timestamps_known(self) -> bool:
        """False when the source carried no timestamps (e.g. a Zalo Web copy)."""
        return any(m.sent_at is not None for m in self.messages)

    @property
    def speakers_known(self) -> bool:
        """False when the source gave no sender labels, only message bodies."""
        return any(m.direction is not Direction.UNKNOWN for m in self.messages)

    @property
    def started_at(self) -> datetime | None:
        stamps = [m.sent_at for m in self.messages if m.sent_at]
        return min(stamps) if stamps else None

    @property
    def last_message_at(self) -> datetime | None:
        stamps = [m.sent_at for m in self.messages if m.sent_at]
        return max(stamps) if stamps else None

    def sort(self) -> None:
        """Chronological where timestamps exist, else the order they were captured in."""
        if self.timestamps_known:
            self.messages.sort(key=lambda m: (m.sent_at is None, m.sent_at, m.sequence))
        else:
            self.messages.sort(key=lambda m: m.sequence)

    def transcript(self, *, include_timestamps: bool = True, tz: tzinfo | None = None) -> str:
        """Flatten to the plain text handed to the LLM.

        Storage is always UTC, but the transcript is rendered in `tz` (the business's
        local time) so the model reports times that match what a human sees in the app.
        """
        lines = []
        for m in self.messages:
            body = m.text.strip()
            if m.attachments:
                tags = ", ".join(a.kind for a in m.attachments)
                body = f"{body} <attachment: {tags}>".strip()
            if not body:
                continue

            stamp = ""
            if include_timestamps and m.sent_at is not None:
                local = m.sent_at.astimezone(tz) if tz else m.sent_at
                stamp = f"[{local:%Y-%m-%d %H:%M}] "

            if m.direction is Direction.UNKNOWN and not m.sender_name:
                # No speaker label available. Emit the bare line rather than
                # guessing a role -- the extraction step infers it from context.
                lines.append(f"{stamp}{body}")
            else:
                who = m.sender_name or (
                    "Customer" if m.direction is Direction.INBOUND else "Agent"
                )
                lines.append(f"{stamp}{who}: {body}")
        return "\n".join(lines)

    def content_hash(self) -> str:
        """Stable fingerprint of conversation content, for extraction caching."""
        payload = json.dumps(
            [
                [m.message_id,
                 m.sent_at.isoformat() if m.sent_at else None,
                 m.sequence, m.direction.value, m.text]
                for m in self.messages
            ],
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
    schema_hash: str = ""
    prompt_version: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
