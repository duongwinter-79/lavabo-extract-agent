"""Zalo OA connector: orders posted in an OA-owned (GMF) group.

Zalo pushes group messages to a webhook; there is no history endpoint for a group, so
`scripts/lavabo_webhook.py` receives them and stores each delivery verbatim in
`oa_events`. This connector turns the ones that look like orders into Conversations.

Why it is worth the setup: the payload carries the sender, so `Người chốt đơn` stops
being a per-session guess, and capture becomes automatic -- nobody copies anything.

An OA can only do this for a group it owns. It cannot read an ordinary staff group,
so the team has to be moved into an OA-managed group first. See docs/07-zalo-oa-flow.md.

WIRE FORMAT NOT VERIFIED. developers.zalo.me was unreachable while this was written,
so the field names below are a best reading of Zalo's webhook conventions rather than
something confirmed against the docs. Everything Zalo-specific is confined to
`EVENT_FIELDS` and `parse_event` -- correcting them should not touch anything else.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterator

from ..models import Conversation, Direction, Message, Source
from .zalo_export import ORDER_HEADER, _detect_attachments

log = logging.getLogger(__name__)

# Candidate paths for each field, tried in order. Zalo nests differently across event
# types, so several are listed rather than assuming one shape.
EVENT_FIELDS: dict[str, list[tuple[str, ...]]] = {
    "event_id":    [("msg_id",), ("message", "msg_id"), ("event_name_id",), ("id",)],
    "group_id":    [("group_id",), ("recipient", "id"), ("group", "id")],
    "sender_id":   [("sender", "id"), ("from_id",), ("user_id_by_app",)],
    "sender_name": [("sender", "display_name"), ("sender", "name"),
                    ("user_name",), ("display_name",)],
    "text":        [("message", "text"), ("message", "msg"), ("text",), ("msg",)],
    "timestamp":   [("timestamp",), ("message", "timestamp"), ("time",)],
}

# Group message events. Anything else (joins, renames, reactions) is stored but not
# turned into an order.
MESSAGE_EVENTS = {
    "user_send_text_group", "oa_send_text_group", "group_send_text",
    "user_send_text", "message_group",
}


def _dig(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _first(payload: dict[str, Any], field: str) -> Any:
    for path in EVENT_FIELDS.get(field, []):
        if (value := _dig(payload, path)) not in (None, ""):
            return value
    return None


def parse_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten one webhook delivery into the columns `oa_events` stores.

    Returns None when the payload carries no usable identity, which is the only case
    worth dropping: without an id, retries would duplicate.
    """
    event_id = _first(payload, "event_id")
    if not event_id:
        return None

    stamp = _first(payload, "timestamp")
    sent_at = None
    if stamp is not None:
        try:
            millis = int(stamp)
            # Zalo sends epoch milliseconds; treat a seconds-sized value as seconds.
            sent_at = datetime.fromtimestamp(
                millis / 1000 if millis > 10**11 else millis, tz=timezone.utc
            ).isoformat()
        except (TypeError, ValueError):
            sent_at = None

    return {
        "event_id": str(event_id),
        "group_id": str(_first(payload, "group_id") or ""),
        "sender_id": str(_first(payload, "sender_id") or ""),
        "sender_name": _first(payload, "sender_name") or None,
        "sent_at": sent_at,
        "text": _first(payload, "text") or "",
        "payload": payload,
    }


def is_message_event(payload: dict[str, Any]) -> bool:
    name = str(payload.get("event_name") or payload.get("event") or "").lower()
    if name:
        return name in MESSAGE_EVENTS or "text" in name
    return bool(_first(payload, "text"))          # unnamed but carries a body


class ZaloOAConnector:
    """Reads stored webhook events; does not call Zalo itself.

    Keeping the network at the edge means this stays testable without credentials,
    and a wire-format correction is a change to one module.
    """

    source = Source.ZALO_OA

    def __init__(self, events: list[dict], *, month: int | None = None,
                 year: int | None = None) -> None:
        self.events = events
        self.month = month
        self.year = year

    def check(self) -> tuple[bool, str]:
        if not self.events:
            return True, ("zalo_oa: no webhook events stored yet — is "
                          "scripts/lavabo_webhook.py running and reachable?")
        orders = sum(1 for e in self.events if ORDER_HEADER.match((e["text"] or "").strip().splitlines()[0] if (e["text"] or "").strip() else ""))
        return True, f"zalo_oa: {len(self.events)} event(s), {orders} order message(s)"

    def fetch(self) -> Iterator[Conversation]:
        for event in self.events:
            conv = self._build(event)
            if conv:
                yield conv

    def _build(self, event: dict) -> Conversation | None:
        text = (event.get("text") or "").strip()
        if not text:
            return None

        lines = text.splitlines()
        match = ORDER_HEADER.match(lines[0].strip())
        if not match:
            return None                          # ordinary group chatter

        day, month = int(match["day"]), int(match["month"])
        year = int(match["year"]) if match["year"] else None
        if year is not None and year < 100:
            year += 2000

        if self.month is not None and month != self.month:
            return None
        if self.year is not None and (year or self.year) != self.year:
            return None

        sent_at = None
        if event.get("sent_at"):
            try:
                sent_at = datetime.fromisoformat(event["sent_at"])
            except ValueError:
                sent_at = None

        header = lines[0].strip()
        conv = Conversation(
            source=Source.ZALO_OA,
            conversation_id=f"zalo_oa:{event['group_id']}:{day}-{month}-{match['order']}",
            customer_name=(match["customer"] or "").strip() or None,
            origin=f"zalo group {event['group_id']}",
            raw={
                "order_header": header,
                "order_number": int(match["order"]),
                "order_day": day,
                "order_month": month,
                "order_date_text": f"{day}/{month}" + (f"/{match['year']}" if match["year"] else ""),
                "customer_from_header": bool((match["customer"] or "").strip()),
                # The whole point of the OA route: the sender is recorded, so
                # Người chốt đơn is a fact rather than a per-session default.
                "sender_name": event.get("sender_name"),
                "sender_id": event.get("sender_id"),
                "event_id": event["event_id"],
            },
        )
        if year:
            conv.raw["order_year"] = year

        for i, line in enumerate(lines, start=1):
            body = line.strip()
            if not body:
                continue
            conv.messages.append(Message(
                source=Source.ZALO_OA,
                conversation_id=conv.conversation_id,
                message_id=f"{conv.conversation_id}:{i:05d}",
                # One Zalo message is one order, so every line shares its timestamp.
                sent_at=sent_at,
                direction=Direction.OUTBOUND,     # posted by staff in our own group
                sequence=i,
                text=body,
                sender_id=event.get("sender_id"),
                sender_name=event.get("sender_name"),
                attachments=_detect_attachments(body),
                raw={"line": line},
            ))

        log.info("oa order %s from %s (%d lines)", conv.conversation_id,
                 event.get("sender_name") or "?", len(conv.messages))
        return conv
