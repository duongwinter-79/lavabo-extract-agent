"""Who closed each order — the Người chốt đơn column.

That name is not written in the order note; it is who sent the message. A Zalo Web copy
strips sender names, and no API will supply them either: the shop has a Zalo Business
account (an upgraded personal account), which has no API at all — see docs/07. So the
name is asked at capture time, while the operator still knows whose orders these are,
and recorded here beside them.

Two decisions worth keeping:

**It is stored next to the orders, not inside them.** The .txt is hashed for the
extraction cache and sent to the model. Putting a name in it would change the hash, so
correcting a mistyped name would re-run the AI on every affected order and cost quota to
fix a spelling. Kept separate, a correction is free.

**It is per order, not per run.** One paste can hold two people's orders. A run-wide
default is right until it isn't, and when it isn't it moves revenue between staff in the
shop's own SUMIF totals.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

SIDECAR = "_closers.json"
VERSION = 1


def sidecar_path(inbox: Path) -> Path:
    return inbox / SIDECAR


def load(inbox: Path) -> dict[str, str]:
    """filename -> closer. Missing or damaged file reads as empty, never raises.

    A broken sidecar must not stop a capture: the orders are the irreplaceable part,
    and the name can be re-entered.
    """
    path = sidecar_path(inbox)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        orders = data.get("orders") if isinstance(data, dict) else None
        return {str(k): str(v) for k, v in orders.items()} if isinstance(orders, dict) else {}
    except (OSError, ValueError, AttributeError) as exc:
        log.warning("could not read %s (%s) — treating as empty", path.name, exc)
        return {}


def save(inbox: Path, orders: dict[str, str]) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    sidecar_path(inbox).write_text(
        json.dumps({"version": VERSION, "orders": orders}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def record(inbox: Path, filename: str, name: str | None) -> None:
    """Attach a closer to one captured order. A later answer replaces an earlier one."""
    name = (name or "").strip()
    if not name:
        return
    orders = load(inbox)
    if orders.get(filename) == name:
        return
    orders[filename] = name
    save(inbox, orders)


def closer_for(orders: dict[str, str], filename: str) -> str | None:
    return orders.get(filename) or None


def known_names(inbox: Path) -> list[str]:
    """Names used before, most recent first — so the app offers a list, not a text box.

    Typing the name each time is how "Trà My" becomes "Tra My" and stops matching the
    sheet's SUMIF.
    """
    seen: list[str] = []
    for name in reversed(list(load(inbox).values())):
        if name and name not in seen:
            seen.append(name)
    return seen
