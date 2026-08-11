"""SQLite staging layer.

Sits between ingest and extract so the expensive LLM step is never repeated for
content that has not changed. All writes are idempotent upserts.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import Attachment, Conversation, Direction, ExtractionResult, Message, Source

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    source           TEXT NOT NULL,
    conversation_id  TEXT NOT NULL,
    customer_name    TEXT,
    customer_handle  TEXT,
    origin           TEXT,
    raw              TEXT,
    ingested_at      TEXT NOT NULL,
    PRIMARY KEY (source, conversation_id)
);

CREATE TABLE IF NOT EXISTS messages (
    source           TEXT NOT NULL,
    conversation_id  TEXT NOT NULL,
    message_id       TEXT NOT NULL,
    sent_at          TEXT NOT NULL,
    direction        TEXT NOT NULL,
    sender_id        TEXT,
    sender_name      TEXT,
    text             TEXT,
    attachments      TEXT,
    raw              TEXT,
    PRIMARY KEY (source, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages (source, conversation_id, sent_at);

CREATE TABLE IF NOT EXISTS extractions (
    source           TEXT NOT NULL,
    conversation_id  TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    schema_version   INTEGER NOT NULL,
    prompt_version   INTEGER NOT NULL,
    model            TEXT NOT NULL,
    values_json      TEXT NOT NULL,
    confidence_json  TEXT,
    input_tokens     INTEGER DEFAULT 0,
    output_tokens    INTEGER DEFAULT 0,
    error            TEXT,
    extracted_at     TEXT NOT NULL,
    PRIMARY KEY (source, conversation_id, content_hash, schema_version, prompt_version, model)
);

-- Watermarks for incremental Meta pulls; ingested-file hashes for Zalo drops.
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------------------------------------------------------------- writes

    def upsert_conversation(self, conv: Conversation) -> int:
        """Insert/refresh a conversation and its messages. Returns new message count."""
        with self.tx() as c:
            c.execute(
                """INSERT INTO conversations
                     (source, conversation_id, customer_name, customer_handle, origin, raw, ingested_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(source, conversation_id) DO UPDATE SET
                     customer_name   = COALESCE(excluded.customer_name, customer_name),
                     customer_handle = COALESCE(excluded.customer_handle, customer_handle),
                     origin          = COALESCE(excluded.origin, origin),
                     raw             = excluded.raw""",
                (conv.source.value, conv.conversation_id, conv.customer_name,
                 conv.customer_handle, conv.origin,
                 json.dumps(conv.raw, ensure_ascii=False, default=str), _now()),
            )

            before = c.execute(
                "SELECT COUNT(*) FROM messages WHERE source=? AND conversation_id=?",
                (conv.source.value, conv.conversation_id),
            ).fetchone()[0]

            c.executemany(
                """INSERT INTO messages
                     (source, conversation_id, message_id, sent_at, direction,
                      sender_id, sender_name, text, attachments, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source, message_id) DO UPDATE SET
                     text = excluded.text, attachments = excluded.attachments""",
                [
                    (m.source.value, m.conversation_id, m.message_id, m.sent_at.isoformat(),
                     m.direction.value, m.sender_id, m.sender_name, m.text,
                     json.dumps([asdict(a) for a in m.attachments], ensure_ascii=False),
                     json.dumps(m.raw, ensure_ascii=False, default=str))
                    for m in conv.messages
                ],
            )

            after = c.execute(
                "SELECT COUNT(*) FROM messages WHERE source=? AND conversation_id=?",
                (conv.source.value, conv.conversation_id),
            ).fetchone()[0]

        return after - before

    def save_extraction(self, res: ExtractionResult, content_hash: str) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT OR REPLACE INTO extractions
                     (source, conversation_id, content_hash, schema_version, prompt_version,
                      model, values_json, confidence_json, input_tokens, output_tokens,
                      error, extracted_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (res.source.value, res.conversation_id, content_hash, res.schema_version,
                 res.prompt_version, res.model,
                 json.dumps(res.values, ensure_ascii=False, default=str),
                 json.dumps(res.confidence, ensure_ascii=False),
                 res.input_tokens, res.output_tokens, res.error, _now()),
            )

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO state (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ----------------------------------------------------------------- reads

    def conversations(self, source: Source | None = None) -> list[Conversation]:
        sql = "SELECT * FROM conversations"
        params: tuple = ()
        if source:
            sql += " WHERE source=?"
            params = (source.value,)

        out = []
        for row in self.conn.execute(sql + " ORDER BY conversation_id", params).fetchall():
            conv = Conversation(
                source=Source(row["source"]),
                conversation_id=row["conversation_id"],
                customer_name=row["customer_name"],
                customer_handle=row["customer_handle"],
                origin=row["origin"],
                raw=json.loads(row["raw"] or "{}"),
            )
            for m in self.conn.execute(
                "SELECT * FROM messages WHERE source=? AND conversation_id=? ORDER BY sent_at",
                (row["source"], row["conversation_id"]),
            ):
                conv.messages.append(Message(
                    source=Source(m["source"]),
                    conversation_id=m["conversation_id"],
                    message_id=m["message_id"],
                    sent_at=datetime.fromisoformat(m["sent_at"]),
                    direction=Direction(m["direction"]),
                    sender_id=m["sender_id"],
                    sender_name=m["sender_name"],
                    text=m["text"] or "",
                    attachments=[Attachment(**a) for a in json.loads(m["attachments"] or "[]")],
                    raw=json.loads(m["raw"] or "{}"),
                ))
            out.append(conv)
        return out

    def cached_extraction(
        self, conv: Conversation, *, schema_version: int, prompt_version: int, model: str
    ) -> ExtractionResult | None:
        row = self.conn.execute(
            """SELECT * FROM extractions
               WHERE source=? AND conversation_id=? AND content_hash=?
                 AND schema_version=? AND prompt_version=? AND model=? AND error IS NULL""",
            (conv.source.value, conv.conversation_id, conv.content_hash(),
             schema_version, prompt_version, model),
        ).fetchone()
        if not row:
            return None
        return ExtractionResult(
            conversation_id=row["conversation_id"],
            source=Source(row["source"]),
            values=json.loads(row["values_json"]),
            confidence=json.loads(row["confidence_json"] or "{}"),
            model=row["model"],
            schema_version=row["schema_version"],
            prompt_version=row["prompt_version"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
        )

    def stats(self) -> dict[str, int]:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "conversations": q("SELECT COUNT(*) FROM conversations"),
            "messages": q("SELECT COUNT(*) FROM messages"),
            "extractions": q("SELECT COUNT(*) FROM extractions WHERE error IS NULL"),
            "extraction_errors": q("SELECT COUNT(*) FROM extractions WHERE error IS NOT NULL"),
        }
