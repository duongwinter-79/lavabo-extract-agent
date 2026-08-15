"""Zalo connector: parses transcript files a human dropped into data/inbox/zalo/.

Why a file-watcher and not an API: Zalo PC's "Export data" is an encrypted, restore-only
backup and there is no per-conversation export. For a personal (non-OA) account, a manual
transcript is the only zero-risk source. See docs/01-source-verification.md.

Two shapes are handled:

1. Structured -- "Name (14:32 25/12/2025): text" -- parsed by the candidate patterns below.
   Add your own to zalo.line_patterns and it is tried first.
2. Plain -- bare message bodies, one per line, no sender and no timestamp. This is what a
   Zalo Web copy produces (confirmed against a real conversation). Speaker and time are
   left unknown rather than guessed; the extraction step infers speakers from context.

See docs/03-zalo-runbook.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from ..config import ZaloConfig
from ..models import Attachment, Conversation, Direction, Message, Source

log = logging.getLogger(__name__)

TEXT_SUFFIXES = {".txt", ".md", ".log"}
JSON_SUFFIXES = {".json"}
HTML_SUFFIXES = {".html", ".htm"}

# Candidate transcript line formats, most specific first. Named groups: name, ts, text.
DEFAULT_PATTERNS = [
    # Nguyen Van A (14:32 25/12/2025): xin chao
    r"^(?P<name>[^:(\[]{1,60})\s*\((?P<ts>[\d/:\s\-\.]{8,25})\)\s*:\s*(?P<text>.*)$",
    # [25/12/2025 14:32] Nguyen Van A: xin chao
    r"^\[(?P<ts>[\d/:\s\-\.]{8,25})\]\s*(?P<name>[^:]{1,60})\s*:\s*(?P<text>.*)$",
    # 25/12/2025 14:32 - Nguyen Van A: xin chao
    r"^(?P<ts>[\d/\-\.]{8,10}[\s,]+[\d:]{4,8})\s*[-–]\s*(?P<name>[^:]{1,60})\s*:\s*(?P<text>.*)$",
    # 14:32, 25/12/2025 Nguyen Van A: xin chao
    r"^(?P<ts>[\d:]{4,8},?\s*[\d/\-\.]{8,10})\s+(?P<name>[^:]{1,60})\s*:\s*(?P<text>.*)$",
]

TIMESTAMP_FORMATS = [
    "%H:%M %d/%m/%Y", "%H:%M:%S %d/%m/%Y", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S",
    "%d-%m-%Y %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M",
    "%H:%M, %d/%m/%Y", "%H:%M %d-%m-%Y",
]

# Order notes open with a header carrying three facts, e.g.
#     "15/8 - đơn 4"                 -> date + order number
#     "15/8 đơn 1 - Meloxicam"       -> date + order number + Zalo display name
# These are parsed here rather than asked of the model: they are unambiguous, so a
# regex is exactly right and costs nothing, while an LLM would merely be probably right.
ORDER_HEADER = re.compile(
    r"^\s*(?P<day>\d{1,2})\s*[/.\-]\s*(?P<month>\d{1,2})"
    r"(?:\s*[/.\-]\s*(?P<year>\d{2,4}))?"
    r"\s*[-–—,]?\s*"
    r"(?:đơn|don|dơn)\s*(?:hàng\s*)?(?P<order>\d+)"
    r"\s*(?:[-–—:]\s*(?P<customer>\S.*?))?\s*$",
    re.IGNORECASE,
)

ATTACHMENT_MARKERS = {
    "image": ["[hình ảnh]", "[image]", "[photo]", "[ảnh]"],
    "file": ["[tệp]", "[file]", "[đính kèm]", "[attachment]"],
    "video": ["[video]"],
    "audio": ["[tin nhắn thoại]", "[voice]", "[audio]"],
    "sticker": ["[sticker]", "[nhãn dán]"],
}


class ZaloExportConnector:
    source = Source.ZALO

    def __init__(self, config: ZaloConfig, *, processed: set[str] | None = None) -> None:
        self.config = config
        self.tz = ZoneInfo(config.timezone)
        self.patterns = [re.compile(p) for p in (config.line_patterns or []) + DEFAULT_PATTERNS]
        self.own = {n.strip().casefold() for n in config.own_names if n.strip()}
        self.processed = processed or set()
        self.seen_hashes: set[str] = set()

    def check(self) -> tuple[bool, str]:
        if not self.config.inbox_dir.exists():
            return False, f"zalo: inbox {self.config.inbox_dir} does not exist"
        n = len(self._files())
        if not self.own:
            return True, f"zalo: {n} file(s) found — WARNING: zalo.own_names is empty, every message will be read as inbound"
        return True, f"zalo: {n} file(s) in {self.config.inbox_dir}"

    def _files(self) -> list[Path]:
        allowed = TEXT_SUFFIXES | JSON_SUFFIXES | HTML_SUFFIXES
        return sorted(p for p in self.config.inbox_dir.rglob("*")
                      if p.is_file() and p.suffix.lower() in allowed)

    def fetch(self) -> Iterator[Conversation]:
        for path in self._files():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            if digest in self.processed:
                log.info("skip %s (already ingested)", path.name)
                continue
            self.seen_hashes.add(digest)

            try:
                conv = self.parse_file(path, digest)
            except Exception as exc:
                log.error("failed to parse %s: %s", path.name, exc)
                continue

            if conv and conv.messages:
                yield conv
            else:
                log.warning("%s: no messages parsed — check zalo.line_patterns", path.name)

    # --------------------------------------------------------------- parsing

    def parse_file(self, path: Path, digest: str) -> Conversation | None:
        suffix = path.suffix.lower()
        text = path.read_text(encoding="utf-8", errors="replace")

        if suffix in JSON_SUFFIXES:
            return self._parse_json(path, digest, text)
        if suffix in HTML_SUFFIXES:
            text = _strip_html(text)
        return self._parse_transcript(path, digest, text)

    def _conversation(self, path: Path, digest: str) -> Conversation:
        # Filename is the conversation identity: "Nguyen Van A.txt" -> customer name.
        return Conversation(
            source=Source.ZALO,
            conversation_id=f"zalo:{path.stem}:{digest}",
            customer_name=path.stem.strip(),
            origin=str(path.name),
            raw={"file": str(path), "sha256_16": digest},
        )

    def _parse_plain(self, path: Path, digest: str, lines: list[str]) -> Conversation:
        """Bare lines with no sender and no timestamp.

        Covers both shapes this shop actually copies: a Zalo Web conversation with
        the speaker labels stripped, and a single structured note such as an order
        written as one message. Nothing is inferred here -- speaker and time stay
        unknown and the extraction step, which sees the whole block, decides which
        shape it is. A regex cannot make that call; a model reading it can.
        """
        conv = self._conversation(path, digest)
        conv.raw["format"] = "plain"
        self._read_order_header(conv, lines)

        seq = 0
        for line in lines:
            body = line.strip()
            if not body:
                continue
            seq += 1
            conv.messages.append(Message(
                source=Source.ZALO,
                conversation_id=conv.conversation_id,
                message_id=f"{conv.conversation_id}:{seq:05d}",
                sent_at=None,                 # genuinely absent; never fabricated
                direction=Direction.UNKNOWN,  # resolved during extraction
                sequence=seq,
                text=body,
                attachments=_detect_attachments(body),
                raw={"line": line},
            ))

        log.info("%s: no sender/timestamp structure — read %d content line(s); whether this "
                 "is an unlabelled conversation or a single note is resolved at extraction "
                 "time", path.name, seq)
        return conv

    def _read_order_header(self, conv: Conversation, lines: list[str]) -> None:
        """Pull date, order number and customer display name off the first line.

        Populates conversation fields directly, so these never depend on the model
        getting them right. Absent or unrecognised headers are simply left alone --
        the filename stays the fallback identity.
        """
        head = next((ln.strip() for ln in lines if ln.strip()), "")
        match = ORDER_HEADER.match(head)
        if not match:
            return

        day, month = int(match["day"]), int(match["month"])
        conv.raw["order_header"] = head
        conv.raw["order_number"] = int(match["order"])
        conv.raw["order_day"] = day
        conv.raw["order_month"] = month
        # Year is usually omitted. Record what was written; do not invent one.
        if match["year"]:
            year = int(match["year"])
            conv.raw["order_year"] = year + 2000 if year < 100 else year
        conv.raw["order_date_text"] = f"{day}/{month}" + (f"/{match['year']}" if match["year"] else "")

        if customer := (match["customer"] or "").strip():
            conv.customer_name = customer
            conv.raw["customer_from_header"] = True

        log.info("order header: date=%s order=%s customer=%s",
                 conv.raw["order_date_text"], conv.raw["order_number"],
                 conv.customer_name or "(not in header)")

    def _parse_transcript(self, path: Path, digest: str, text: str) -> Conversation:
        lines = [ln.rstrip() for ln in text.splitlines()]
        pattern, hits = self._best_pattern(lines)

        nonblank = sum(1 for ln in lines if ln.strip())
        if not nonblank:
            log.warning("%s: file is empty", path.name)
            return self._conversation(path, digest)

        rate = hits / nonblank if nonblank else 0.0

        # Below this, the file has no per-line sender/time structure to speak of,
        # so treating each line as a bare message beats forcing a bad regex onto it.
        if pattern is None or rate < 0.3:
            return self._parse_plain(path, digest, lines)

        log.info("%s: pattern matched %d/%d lines (%.0f%%)", path.name, hits, nonblank, rate * 100)
        if rate < 0.5:
            log.warning(
                "%s: low match rate (%.0f%%). The transcript format likely differs from the "
                "built-in patterns — add the right regex to zalo.line_patterns "
                "(docs/03-zalo-runbook.md).", path.name, rate * 100,
            )

        conv = self._conversation(path, digest)
        current: Message | None = None
        seq = 0

        for line in lines:
            match = pattern.match(line)
            if match:
                sent_at = self._parse_ts(match.group("ts"))
                if sent_at is None:
                    log.debug("unparseable timestamp %r", match.group("ts"))
                    if current:  # treat as a continuation rather than dropping content
                        current.text += "\n" + line
                    continue

                name = match.group("name").strip()
                body = match.group("text")
                seq += 1
                current = Message(
                    source=Source.ZALO,
                    conversation_id=conv.conversation_id,
                    message_id=f"{conv.conversation_id}:{seq:05d}",
                    sent_at=sent_at,
                    sequence=seq,
                    direction=(Direction.OUTBOUND if name.casefold() in self.own else Direction.INBOUND),
                    text=body,
                    sender_name=name,
                    attachments=_detect_attachments(body),
                    raw={"line": line},
                )
                conv.messages.append(current)
            elif current and line.strip():
                # Multi-line message body.
                current.text += "\n" + line

        for m in conv.messages:
            m.text = m.text.strip()
            m.attachments = _detect_attachments(m.text)

        conv.sort()
        return conv

    def _parse_json(self, path: Path, digest: str, text: str) -> Conversation:
        """For a JSON transcript — including whatever a future probe finds inside a backup."""
        data: Any = json.loads(text)
        records = data.get("messages", data) if isinstance(data, dict) else data
        if not isinstance(records, list):
            raise ValueError("JSON transcript must be a list, or an object with a 'messages' list")

        conv = self._conversation(path, digest)
        for i, rec in enumerate(records, 1):
            raw_ts = rec.get("timestamp") or rec.get("time") or rec.get("sent_at") or rec.get("created_time")
            sent_at = self._parse_ts(str(raw_ts)) if raw_ts is not None else None
            if sent_at is None:
                log.warning("%s record %d: unparseable timestamp %r, skipping", path.name, i, raw_ts)
                continue

            name = str(rec.get("sender") or rec.get("from") or rec.get("name") or "").strip()
            body = str(rec.get("text") or rec.get("message") or rec.get("content") or "")
            conv.messages.append(Message(
                source=Source.ZALO,
                conversation_id=conv.conversation_id,
                message_id=f"{conv.conversation_id}:{rec.get('id', i)}",
                sent_at=sent_at,
                sequence=i,
                direction=(Direction.OUTBOUND if name.casefold() in self.own else Direction.INBOUND),
                text=body,
                sender_name=name or None,
                attachments=_detect_attachments(body),
                raw=rec if isinstance(rec, dict) else {"value": rec},
            ))

        conv.sort()
        return conv

    def _best_pattern(self, lines: list[str]) -> tuple[re.Pattern | None, int]:
        best, best_hits = None, 0
        for pat in self.patterns:
            hits = sum(1 for ln in lines if pat.match(ln))
            if hits > best_hits:
                best, best_hits = pat, hits
        return best, best_hits

    def _parse_ts(self, value: str) -> datetime | None:
        """Zalo transcripts carry naive local time — attach the configured timezone."""
        value = value.strip()
        if not value:
            return None

        # Epoch seconds/millis (likely inside any JSON we recover from a backup).
        if value.isdigit():
            n = int(value)
            if n > 10**12:
                n //= 1000
            if 10**8 < n < 10**11:
                return datetime.fromtimestamp(n, tz=self.tz).astimezone(ZoneInfo("UTC"))

        normalized = re.sub(r"\s+", " ", value)
        for fmt in TIMESTAMP_FORMATS:
            try:
                return datetime.strptime(normalized, fmt).replace(tzinfo=self.tz).astimezone(ZoneInfo("UTC"))
            except ValueError:
                continue
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        return (dt.replace(tzinfo=self.tz) if dt.tzinfo is None else dt).astimezone(ZoneInfo("UTC"))


def _detect_attachments(text: str) -> list[Attachment]:
    low = text.casefold()
    return [Attachment(kind=kind)
            for kind, markers in ATTACHMENT_MARKERS.items()
            if any(m in low for m in markers)]


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr)>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", "", html)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, char)
    return "\n".join(ln.strip() for ln in text.splitlines())
