"""Who posted each order message — người báo đơn.

Not the same person as Người chốt đơn, though often the same name. The closer is typed by
whoever is capturing; the reporter is whoever actually sent the message in the group, and
it comes out of the message itself once the chat carries sender names.

It exists for one reason: the order key. An order was identified by (ngày, tháng, số đơn),
which holds only while a single person posts orders. Two people both numbering their own
orders from 1 collide on day one -- "13/7 đơn 1" from each of them is two different orders
for two different customers, and the second silently merged into the first or was filed as
a competing version of it. Adding the reporter separates them.

Stored beside the orders rather than inside them, like closers.py and for the same reason:
the .txt is hashed for the extraction cache, so writing a sender line into it would re-run
the model over every order to record something the model is never asked about.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

SIDECAR = "_reporters.json"
VERSION = 1


def sidecar_path(inbox: Path) -> Path:
    return inbox / SIDECAR


def load(inbox: Path) -> dict[str, str]:
    """filename -> reporter. Missing or damaged file reads as empty, never raises."""
    path = sidecar_path(inbox)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        orders = data.get("orders") if isinstance(data, dict) else None
        if not isinstance(orders, dict):
            return {}
        return {str(k): str(v) for k, v in orders.items() if str(v).strip()}
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
    """Remember who reported this order. A blank name leaves any stored one alone.

    Never overwrites a known reporter with an unknown one: the same order re-pasted from a
    copy that lost its sender names must not erase what an earlier, richer paste knew.
    """
    name = (name or "").strip()
    if not name:
        return
    orders = load(inbox)
    if orders.get(filename) == name:
        return
    orders[filename] = name
    save(inbox, orders)


def reporter_for(orders: dict[str, str], filename: str) -> str | None:
    return orders.get(filename) or None
