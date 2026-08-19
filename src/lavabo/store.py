"""SQLite staging layer.

Sits between ingest and extract so the expensive LLM step is never repeated for
content that has not changed. All writes are idempotent upserts.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import Attachment, Conversation, Direction, ExtractionResult, Message, Source

log = logging.getLogger(__name__)

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
    sent_at          TEXT,               -- NULL when the source carries no timestamps
    sequence         INTEGER NOT NULL DEFAULT 0,
    direction        TEXT NOT NULL,
    sender_id        TEXT,
    sender_name      TEXT,
    text             TEXT,
    attachments      TEXT,
    raw              TEXT,
    PRIMARY KEY (source, message_id)
);

CREATE TABLE IF NOT EXISTS extractions (
    source           TEXT NOT NULL,
    conversation_id  TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    schema_version   INTEGER NOT NULL,
    schema_hash      TEXT NOT NULL DEFAULT '',
    prompt_version   INTEGER NOT NULL,
    model            TEXT NOT NULL,
    values_json      TEXT NOT NULL,
    confidence_json  TEXT,
    input_tokens     INTEGER DEFAULT 0,
    output_tokens    INTEGER DEFAULT 0,
    error            TEXT,
    extracted_at     TEXT NOT NULL,
    PRIMARY KEY (source, conversation_id, content_hash, schema_version, schema_hash,
                 prompt_version, model)
);

-- Raw Zalo OA webhook deliveries, kept verbatim. Zalo pushes once and does not
-- offer group history, so an event discarded here cannot be fetched again.
CREATE TABLE IF NOT EXISTS oa_events (
    event_id     TEXT PRIMARY KEY,
    group_id     TEXT,
    sender_id    TEXT,
    sender_name  TEXT,
    sent_at      TEXT,
    text         TEXT,
    payload      TEXT NOT NULL,
    received_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oa_events_group ON oa_events (group_id, sent_at);

-- Watermarks for incremental Meta pulls; ingested-file hashes for Zalo drops.
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Indexes are created after migrations, since a legacy table may lack the columns.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages (source, conversation_id, sequence);
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
        self._migrate()
        self._migrate_zalo_ids()
        self.conn.executescript(INDEXES)
        self.conn.commit()

    ZALO_DIGEST_ID = re.compile(r"^(zalo:.+):[0-9a-f]{16}$")

    def _migrate_zalo_ids(self) -> None:
        """Rewrite Zalo conversation ids that still carry a content digest.

        The id used to be "zalo:{filename}:{digest}", so rewriting a file minted a second
        conversation and the export wrote both. The digest is gone from the id now, and
        these rows are MIGRATED rather than dropped: the extractions hang off the
        conversation id, and deleting them would charge for a whole month of re-extraction
        to fix a bookkeeping mistake. Which is exactly what an earlier version of this fix
        did, by pruning them instead.
        """
        rows = [r[0] for r in self.conn.execute(
            "SELECT conversation_id FROM conversations WHERE source='zalo'")]
        moves = [(old, m.group(1)) for old in rows if (m := self.ZALO_DIGEST_ID.match(old))]
        if not moves:
            return

        existing = set(rows)
        with self.tx() as c:
            for old_id, new_id in moves:
                if new_id in existing:
                    # A row under the stable id already exists, so this one is the older
                    # copy of the same file. Its extractions cannot be better than the
                    # ones already there.
                    for table in ("messages", "extractions", "conversations"):
                        c.execute(f"DELETE FROM {table} WHERE source='zalo' "
                                  "AND conversation_id=?", (old_id,))
                    continue
                for table in ("conversations", "messages", "extractions"):
                    c.execute(f"UPDATE {table} SET conversation_id=? "
                              "WHERE source='zalo' AND conversation_id=?", (new_id, old_id))
                existing.add(new_id)
        log.warning("migrated %d Zalo conversation(s) to file-based ids, keeping their "
                    "cached extractions", len(moves))

    def _migrate(self) -> None:
        """Bring a database created by an earlier version up to the current schema.

        Databases predating plain-format support have `messages.sent_at NOT NULL` and no
        `sequence` column, so they reject untimed messages. SQLite cannot relax a NOT NULL
        constraint in place, so the table is rebuilt and its rows copied across.
        """
        # extractions: a schema fingerprint joined the cache key. Older rows lack it,
        # so they can never match a current lookup -- which is the correct outcome,
        # since they were produced under a schema we can no longer identify.
        ext = {r[1] for r in self.conn.execute("PRAGMA table_info(extractions)")}
        if ext and "schema_hash" not in ext:
            log.warning("adding extractions.schema_hash; existing cached extractions "
                        "will be re-run once, as their schema cannot be identified")
            with self.tx() as c:
                c.execute("ALTER TABLE extractions ADD COLUMN schema_hash TEXT NOT NULL DEFAULT ''")

        cols = {r[1]: r for r in self.conn.execute("PRAGMA table_info(messages)")}
        if not cols:
            return                                  # fresh database, nothing to migrate

        sent_at_not_null = bool(cols["sent_at"][3]) if "sent_at" in cols else False
        if "sequence" in cols and not sent_at_not_null:
            return                                  # already current

        log.warning("migrating staging db to the current schema (messages table rebuild)")

        carried = [c for c in ("source", "conversation_id", "message_id", "sent_at",
                               "direction", "sender_id", "sender_name", "text",
                               "attachments", "raw") if c in cols]
        seq = "sequence" if "sequence" in cols else "0"

        with self.tx() as c:
            c.executescript("""
                CREATE TABLE messages_migrated (
                    source           TEXT NOT NULL,
                    conversation_id  TEXT NOT NULL,
                    message_id       TEXT NOT NULL,
                    sent_at          TEXT,
                    sequence         INTEGER NOT NULL DEFAULT 0,
                    direction        TEXT NOT NULL,
                    sender_id        TEXT,
                    sender_name      TEXT,
                    text             TEXT,
                    attachments      TEXT,
                    raw              TEXT,
                    PRIMARY KEY (source, message_id)
                );
            """)
            c.execute(
                f"INSERT INTO messages_migrated ({', '.join(carried)}, sequence) "
                f"SELECT {', '.join(carried)}, {seq} FROM messages"
            )
            c.executescript(
                "DROP TABLE messages;"
                "ALTER TABLE messages_migrated RENAME TO messages;"
            )

        moved = self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        log.warning("migration complete, %d message(s) preserved", moved)

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

    def upsert_conversation(self, conv: Conversation, *,
                            replace_messages: bool = False) -> int:
        """Insert/refresh a conversation and its messages. Returns new message count.

        `replace_messages` drops any message of this conversation that the incoming set no
        longer carries. Right for a Zalo order file, which IS the whole conversation, so a
        file that gets shorter -- a retrim, a resegment -- must not leave the lines it lost
        behind. Wrong for Meta, which pages history in incrementally and would be deleting
        the messages it has not fetched yet, so it stays off by default.
        """
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

            if replace_messages:
                keep = [m.message_id for m in conv.messages]
                placeholders = ",".join("?" * len(keep))
                c.execute(
                    "DELETE FROM messages WHERE source=? AND conversation_id=?"
                    + (f" AND message_id NOT IN ({placeholders})" if keep else ""),
                    (conv.source.value, conv.conversation_id, *keep),
                )

            c.executemany(
                """INSERT INTO messages
                     (source, conversation_id, message_id, sent_at, sequence, direction,
                      sender_id, sender_name, text, attachments, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source, message_id) DO UPDATE SET
                     text = excluded.text, attachments = excluded.attachments""",
                [
                    (m.source.value, m.conversation_id, m.message_id,
                     m.sent_at.isoformat() if m.sent_at else None, m.sequence,
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

    def prune_conversations(self, source: Source, keep_stems: set[str],
                            *, identify) -> int:
        """Drop conversations of `source` whose file is gone, with their messages.

        Compared by FILE, through `identify`, and not by whole conversation id. The first
        version of this compared ids, so when the id scheme changed every existing row
        looked orphaned and a single ingest deleted a staged month -- conversations,
        messages and the cached extractions with them. Nothing about "this file is gone"
        should depend on how its id happened to be spelled.
        """
        if not keep_stems:
            return 0                    # never prune to nothing on an empty listing
        with self.tx() as c:
            stale = [row[0] for row in c.execute(
                "SELECT conversation_id FROM conversations WHERE source=?",
                (source.value,)) if identify(row[0]) not in keep_stems]
            if not stale:
                return 0
            marks = ",".join("?" * len(stale))
            for table in ("messages", "extractions", "conversations"):
                c.execute(f"DELETE FROM {table} WHERE source=? "
                          f"AND conversation_id IN ({marks})", (source.value, *stale))
        log.info("pruned %d conversation(s) with no file behind them", len(stale))
        return len(stale)

    def save_extraction(self, res: ExtractionResult, content_hash: str) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT OR REPLACE INTO extractions
                     (source, conversation_id, content_hash, schema_version, schema_hash,
                      prompt_version, model, values_json, confidence_json, input_tokens,
                      output_tokens, error, extracted_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (res.source.value, res.conversation_id, content_hash, res.schema_version,
                 res.schema_hash, res.prompt_version, res.model,
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
                "SELECT * FROM messages WHERE source=? AND conversation_id=? ORDER BY sequence, sent_at",
                (row["source"], row["conversation_id"]),
            ):
                conv.messages.append(Message(
                    source=Source(m["source"]),
                    conversation_id=m["conversation_id"],
                    message_id=m["message_id"],
                    sent_at=datetime.fromisoformat(m["sent_at"]) if m["sent_at"] else None,
                    sequence=m["sequence"],
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
        self, conv: Conversation, *, schema_version: int, schema_hash: str,
        prompt_version: int, model: str
    ) -> ExtractionResult | None:
        row = self.conn.execute(
            """SELECT * FROM extractions
               WHERE source=? AND conversation_id=? AND content_hash=?
                 AND schema_version=? AND schema_hash=? AND prompt_version=? AND model=?
                 AND error IS NULL""",
            (conv.source.value, conv.conversation_id, conv.content_hash(),
             schema_version, schema_hash, prompt_version, model),
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
            schema_hash=row["schema_hash"],
            prompt_version=row["prompt_version"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
        )

    def save_oa_event(self, event: dict) -> bool:
        """Store one webhook delivery. Returns False if we had it already.

        Zalo can retry a delivery, so the event id is the primary key and a repeat is
        a no-op rather than a duplicate order.
        """
        with self.tx() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO oa_events
                     (event_id, group_id, sender_id, sender_name, sent_at, text,
                      payload, received_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (event["event_id"], event.get("group_id"), event.get("sender_id"),
                 event.get("sender_name"), event.get("sent_at"), event.get("text"),
                 json.dumps(event.get("payload", {}), ensure_ascii=False), _now()),
            )
        return cur.rowcount > 0

    def oa_events(self, *, since: str | None = None) -> list[dict]:
        sql = "SELECT * FROM oa_events"
        params: tuple = ()
        if since:
            sql += " WHERE received_at > ?"
            params = (since,)
        rows = self.conn.execute(sql + " ORDER BY sent_at, event_id", params).fetchall()
        return [dict(r) for r in rows]

    def latest_extraction_rows(self, conversation_id: str) -> list[dict]:
        """Every stored attempt for a conversation, newest first, errors included.

        cached_extraction() deliberately ignores failed rows; diagnostics need exactly
        those, since a silent failure is what makes an output look merely incomplete.
        """
        rows = self.conn.execute(
            "SELECT * FROM extractions WHERE conversation_id=? ORDER BY extracted_at DESC",
            (conversation_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, int]:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "conversations": q("SELECT COUNT(*) FROM conversations"),
            "messages": q("SELECT COUNT(*) FROM messages"),
            "extractions": q("SELECT COUNT(*) FROM extractions WHERE error IS NULL"),
            "extraction_errors": q("SELECT COUNT(*) FROM extractions WHERE error IS NOT NULL"),
        }
