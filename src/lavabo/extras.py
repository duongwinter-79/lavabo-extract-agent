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
import re
import unicodedata
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


# Read against the diacritic-folded text, MOST SPECIFIC FIRST, because these overlap:
# "tổng thu hộ 13.800" contains both "tổng" and "thu", and reporting it three times under
# three labels is worse than not reporting it. Each match blanks its own span (see
# amounts), so a later, broader pattern cannot re-read ground already claimed.
#
# The amount itself is captured loosely and handed to parse_vnd rather than matched
# precisely here: it already knows "1tr8" is 1,800,000 and "13.800" is 13,800,000, and a
# second grammar for the same shorthand would only be a second thing to get wrong. A
# tight [\d.,]+ read "1tr8" as 1 -- off by 800,000 on a real order.
#
# The (?<!guong ) guard is the one DEPOSIT_ANY also needs: "gương cộc" folds to the same
# letters as "cọc", and a product name is not a deposit.
_AMOUNT_PATTERNS = [
    (re.compile(r"to+ng[rsfjx]?\s+thu\s+ho\b([^\n]{0,20})"), "thu hộ"),
    (re.compile(r"thu\s+them\b([^\n]{0,20})"), "thu thêm"),
    (re.compile(r"\bto+ng[rsfjx]?\b([^\n]{0,20})"), "Tổng"),
    (re.compile(r"\b(?:da\s+)?(?<!guong )coc\b([^\n]{0,20})"), "cọc"),
    (re.compile(r"\bthu\b([^\n]{0,20})"), "thu"),
]


def amounts(text: str) -> str:
    """Every money figure stated in a later message, labelled and converted.

    A reading aid, not a decision: this lands in its own review column and never in
    Tổng/Cọc/Xe thu hộ, so nothing here can reach =SUM or =SUMIF. The operator sees
    "Tổng 12.000.000" beside the order's own 5.800.000 and picks; the app does not,
    because these figures do not reliably reconcile against the shop's own workbook.

    Each label is reported once, from its first occurrence -- a revision restating a
    deposit it already stated is one deposit, not two.
    """
    from .money import format_vnd, parse_vnd          # local: avoids an import cycle

    working = _fold(text)
    out: list[str] = []
    for pattern, label in _AMOUNT_PATTERNS:
        if not (m := pattern.search(working)):
            continue
        if (value := parse_vnd(m.group(1))) is None:
            continue
        out.append(f"{label} {format_vnd(value)}")
        # Blank what this match consumed, so "tổng" cannot re-report the figure that
        # "tổng thu hộ" already claimed.
        working = working[:m.start()] + " " * (m.end() - m.start()) + working[m.end():]
    return " · ".join(out)


def _fold(text: str) -> str:
    """Lowercase, strip Vietnamese tone marks. Mirrors zalo_capture.fold, which lives in
    a script rather than the package and so cannot be imported from here."""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.replace("đ", "d").replace("Đ", "D").lower()
