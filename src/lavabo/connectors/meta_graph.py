"""Meta Graph API connector: Messenger (Page) and Instagram DM.

Endpoints:
    GET /{PAGE_ID}/conversations?platform=messenger&fields=...
    GET /{CONVERSATION_ID}/messages?fields=id,created_time,from,to,message,attachments,...

Both are cursor-paginated; messages come back newest-first and are reversed here.
Requires a Page access token with pages_messaging + pages_read_engagement (and
instagram_manage_messages for IG). Advanced Access is required to see conversations
with people who have no role on the Page -- see docs/04-meta-setup.md.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterator

import requests

from ..config import MetaConfig
from ..models import Attachment, Conversation, Direction, Message, Source

log = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"

CONVERSATION_FIELDS = "id,updated_time,message_count,unread_count,participants,snippet,link"
MESSAGE_FIELDS = "id,created_time,from,to,message,attachments,shares,sticker"

# Graph throttling: code 4 / 17 / 32 / 613 are all "slow down".
THROTTLE_CODES = {4, 17, 32, 613}
MAX_RETRIES = 5


class MetaGraphConnector:
    def __init__(self, config: MetaConfig, platform: str = "messenger",
                 since: datetime | None = None) -> None:
        if platform not in ("messenger", "instagram"):
            raise ValueError(f"unsupported platform {platform!r}")
        self.config = config
        self.platform = platform
        self.since = since
        self.source = Source.MESSENGER if platform == "messenger" else Source.INSTAGRAM
        self.owner_id = config.page_id if platform == "messenger" else (config.instagram_id or config.page_id)
        self.session = requests.Session()

    # ------------------------------------------------------------- transport

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{GRAPH}/{self.config.api_version}/{path}"
        params = {**params, "access_token": self.config.access_token}

        for attempt in range(MAX_RETRIES):
            resp = self.session.get(url, params=params, timeout=60)
            if resp.status_code == 200:
                return resp.json()

            try:
                err = resp.json().get("error", {})
            except ValueError:
                err = {}
            code = err.get("code")

            if resp.status_code == 429 or code in THROTTLE_CODES:
                wait = 2 ** attempt * 5
                log.warning("Graph throttled (code=%s), sleeping %ss", code, wait)
                time.sleep(wait)
                continue

            if resp.status_code >= 500:
                wait = 2 ** attempt
                log.warning("Graph %s, retrying in %ss", resp.status_code, wait)
                time.sleep(wait)
                continue

            raise RuntimeError(
                f"Graph API {resp.status_code}: {err.get('message', resp.text[:300])} "
                f"(code={code}, subcode={err.get('error_subcode')})"
            )

        raise RuntimeError(f"Graph API: giving up on {url} after {MAX_RETRIES} attempts")

    def _paginate(self, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        page = self._get(path, params)
        while True:
            yield from page.get("data", [])
            nxt = page.get("paging", {}).get("next")
            if not nxt:
                return
            page = self._get(nxt, {})

    # ---------------------------------------------------------------- public

    def check(self) -> tuple[bool, str]:
        if not self.owner_id:
            return False, f"{self.platform}: page_id/instagram_id not configured"
        try:
            me = self._get(self.owner_id, {"fields": "id,name"})
        except Exception as exc:
            return False, f"{self.platform}: {exc}"
        return True, f"{self.platform}: authenticated as {me.get('name')} ({me.get('id')})"

    def fetch(self) -> Iterator[Conversation]:
        params = {
            "platform": self.platform,
            "fields": CONVERSATION_FIELDS,
            "limit": self.config.page_size,
        }

        seen = 0
        for thread in self._paginate(f"{self.owner_id}/conversations", params):
            # The conversations edge has no reliable since/until, so filter client-side.
            if self.since:
                updated = _parse_time(thread.get("updated_time"))
                if updated and updated <= self.since:
                    log.debug("skip %s (not updated since watermark)", thread["id"])
                    continue

            yield self._build_conversation(thread)

            seen += 1
            if self.config.max_conversations and seen >= self.config.max_conversations:
                log.info("stopping at max_conversations=%s", self.config.max_conversations)
                return

    # --------------------------------------------------------------- mapping

    def _build_conversation(self, thread: dict[str, Any]) -> Conversation:
        conv_id = thread["id"]
        customer = self._customer(thread)

        conv = Conversation(
            source=self.source,
            conversation_id=conv_id,
            customer_name=customer.get("name"),
            customer_handle=customer.get("username") or customer.get("id"),
            origin=thread.get("link") or f"https://business.facebook.com/latest/inbox/all?selected_item_id={conv_id}",
            raw=thread,
        )

        for msg in self._paginate(
            f"{conv_id}/messages",
            {"fields": MESSAGE_FIELDS, "limit": self.config.message_page_size},
        ):
            built = self._build_message(conv_id, msg)
            if built:
                conv.messages.append(built)

        conv.sort()  # Graph returns newest-first
        log.info("%s %s: %d messages", self.platform, conv_id, len(conv.messages))
        return conv

    def _customer(self, thread: dict[str, Any]) -> dict[str, Any]:
        """First participant that isn't us."""
        for p in thread.get("participants", {}).get("data", []):
            if str(p.get("id")) != str(self.owner_id):
                return p
        return {}

    def _build_message(self, conv_id: str, msg: dict[str, Any]) -> Message | None:
        sent_at = _parse_time(msg.get("created_time"))
        if not sent_at:
            log.warning("message %s has no created_time, skipping", msg.get("id"))
            return None

        sender = msg.get("from") or {}
        sender_id = str(sender.get("id", ""))
        direction = Direction.OUTBOUND if sender_id == str(self.owner_id) else Direction.INBOUND

        attachments = [
            Attachment(
                kind=_attachment_kind(a),
                name=a.get("name") or a.get("file_url", "").rsplit("/", 1)[-1] or None,
                url=(a.get("image_data") or {}).get("url") or a.get("file_url") or a.get("video_data", {}).get("url"),
            )
            for a in msg.get("attachments", {}).get("data", [])
        ]
        if msg.get("sticker"):
            attachments.append(Attachment(kind="sticker", url=msg["sticker"]))
        for share in msg.get("shares", {}).get("data", []):
            attachments.append(Attachment(kind="share", name=share.get("name"), url=share.get("link")))

        return Message(
            source=self.source,
            conversation_id=conv_id,
            message_id=msg["id"],
            sent_at=sent_at,
            direction=direction,
            text=msg.get("message") or "",
            sender_id=sender_id or None,
            sender_name=sender.get("name") or sender.get("username"),
            attachments=attachments,
            raw=msg,
        )


def _attachment_kind(a: dict[str, Any]) -> str:
    mime = (a.get("mime_type") or "").lower()
    if a.get("image_data") or mime.startswith("image/"):
        return "image"
    if a.get("video_data") or mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "file"


def _parse_time(value: str | None) -> datetime | None:
    """Graph returns ISO-8601 with a +0000 offset."""
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.astimezone(timezone.utc)
