"""Later messages about an order already captured -- revisions and add-ons.

An order is not always said once. The shop re-sends a header with different contents
("26/7 đơn 2" at Tổng 5.800, then again at Tổng 12.000 weeks later), and far more often
follows an order with a headerless message that changes it ("Đơn này lấy thêm", "Thu
thêm 2.900", "THAY ĐỔI - Lan Anh"). Both were being lost: the first merged into one file
whose totals then contradicted each other, the second discarded outright by
trim_after_deposit as trailing chatter.

Nothing here interprets any of it. The text is kept verbatim, attached to the order it
followed, and rendered into a column the operator reviews by hand -- because the money in
these messages does not reliably reconcile (checked against the shop's own workbook: one
add-on was applied there, another was not), and a wrong total is worth more than a
missing one only if a human chose it.

Kept beside the orders rather than inside them, like closers.py: the .txt is hashed for
the extraction cache, so appending to it would re-run the model over an order whose own
text never changed.

Two kinds, differing only in how the writer renders them:

    version -- same order header, materially different content. Gets its own flagged row,
               with the money left blank so it cannot enter any total before review.
    update  -- a headerless message following the order. Annotates that order's own row.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

SIDECAR = "_extras.json"
VERSION = 1

Kind = Literal["version", "update"]


def sidecar_path(inbox: Path) -> Path:
    return inbox / SIDECAR


def load(inbox: Path) -> dict[str, list[dict[str, str]]]:
    """filename -> [{kind, text}, ...]. Missing or damaged file reads as empty.

    A broken sidecar must not stop a capture: the orders are the irreplaceable part and
    these annotations can be re-captured by pasting the chat again.
    """
    path = sidecar_path(inbox)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        orders = data.get("orders") if isinstance(data, dict) else None
        if not isinstance(orders, dict):
            return {}
        out: dict[str, list[dict[str, str]]] = {}
        for name, items in orders.items():
            if not isinstance(items, list):
                continue
            kept = [
                {"kind": str(i.get("kind") or "update"), "text": str(i.get("text") or "")}
                for i in items
                if isinstance(i, dict) and str(i.get("text") or "").strip()
            ]
            if kept:
                out[str(name)] = kept
        return out
    except (OSError, ValueError, AttributeError) as exc:
        log.warning("could not read %s (%s) — treating as empty", path.name, exc)
        return {}


def save(inbox: Path, orders: dict[str, list[dict[str, str]]]) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    sidecar_path(inbox).write_text(
        json.dumps({"version": VERSION, "orders": orders}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def record(inbox: Path, filename: str, kind: Kind, text: str) -> bool:
    """Attach one later message to an order. Returns True if it was new.

    Exact-duplicate text is ignored, so re-pasting the same chunk -- the normal way this
    shop captures, in overlapping sweeps -- does not stack the same revision repeatedly.
    """
    text = (text or "").strip()
    if not text:
        return False
    orders = load(inbox)
    items = orders.setdefault(filename, [])
    if any(i["text"].strip() == text for i in items):
        return False
    items.append({"kind": kind, "text": text})
    save(inbox, orders)
    return True


def for_order(extras: dict[str, list[dict[str, str]]], filename: str,
              kind: Kind | None = None) -> list[str]:
    """The texts recorded against one order, optionally of a single kind."""
    items = extras.get(filename) or []
    return [i["text"] for i in items if kind is None or i["kind"] == kind]


def summary(conv_raw: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(updates, versions) for a conversation, as put there by the Zalo connector."""
    items = conv_raw.get("extras") or []
    updates = [i["text"] for i in items if i.get("kind") == "update"]
    versions = [i["text"] for i in items if i.get("kind") == "version"]
    return updates, versions
