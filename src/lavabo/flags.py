"""Per-order review flags that are not about the order's contents.

`extras` holds later messages about an order -- things a person said. This holds things
the SYSTEM knows about how an order was captured, which belong in the same review column
but have no text to show: chiefly that an order was split out by the regex fallback while
AI segmentation was configured, so it never had the benefit of the better segmenter.

Kept in the same shape and for the same reason as closers.py and extras.py: beside the
orders rather than inside them, because the .txt is hashed for the extraction cache and
adding a marker to it would re-run the model over an order whose own text never changed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

SIDECAR = "_flags.json"
VERSION = 1

# The fallback ran: this order was found by the regexes while the model was meant to be
# doing it. Not an error -- the order is captured and its money is intact -- but the
# closed-phrase weaknesses the model exists to fix were in play for this one.
NO_AI = "chưa qua AI"

# Read off a phone screen recording rather than from copied text. The text path can hand
# the model line numbers and slice the words out of the paste itself, so what is saved is
# the shop's own characters by construction. A photograph has no line numbers, so the model
# transcribes -- and a transcription can be subtly wrong in the one place it matters, since
# "5.800" and "5.800.000" differ by a dot. Not an error, and not a reason to refuse the
# order; a reason for somebody to compare the total against the message once.
FROM_VIDEO = "từ video — cần đối chiếu"


def sidecar_path(inbox: Path) -> Path:
    return inbox / SIDECAR


def load(inbox: Path) -> dict[str, list[str]]:
    """filename -> [flag, ...]. Missing or damaged file reads as empty, never raises."""
    path = sidecar_path(inbox)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        orders = data.get("orders") if isinstance(data, dict) else None
        if not isinstance(orders, dict):
            return {}
        return {str(name): [str(f) for f in items if str(f).strip()]
                for name, items in orders.items() if isinstance(items, list)}
    except (OSError, ValueError, AttributeError) as exc:
        log.warning("could not read %s (%s) — treating as empty", path.name, exc)
        return {}


def save(inbox: Path, orders: dict[str, list[str]]) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    sidecar_path(inbox).write_text(
        json.dumps({"version": VERSION, "orders": orders}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def record(inbox: Path, filename: str, flag: str) -> bool:
    """Add one flag to an order. Returns True if it was not already there."""
    if not flag:
        return False
    orders = load(inbox)
    items = orders.setdefault(filename, [])
    if flag in items:
        return False
    items.append(flag)
    save(inbox, orders)
    return True


def clear(inbox: Path, filename: str, flag: str) -> bool:
    """Remove one flag. Used when a later capture supersedes the reason for it --
    re-pasting under a working model upgrades an order the fallback had captured."""
    orders = load(inbox)
    items = orders.get(filename) or []
    if flag not in items:
        return False
    items.remove(flag)
    if items:
        orders[filename] = items
    else:
        orders.pop(filename, None)
    save(inbox, orders)
    return True


def for_order(flags: dict[str, list[str]], filename: str) -> list[str]:
    return flags.get(filename) or []
