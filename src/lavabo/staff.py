"""Names that belong to the shop rather than to a customer.

One header shape cannot be settled by any rule: "13/7 đơn 3 - Anh Tâm - Hà Nội" is either
a customer and the person who reported the order, or a customer and a place. Both are two
names either side of a dash, both are short, and neither contains a digit. "Trà My" is a
staff name here and also a district in Quảng Nam, so no amount of pattern matching decides
it -- only knowing who works at this shop does.

So the question is answered once per NAME and remembered, instead of guessed once per
header. A name confirmed here makes every future header carrying it exact, and the
adjudication -- whether it came from a person picking the name in the closer list or from
the model reading the headers -- never has to run for that name again.

Kept beside the orders, like the other sidecars, and for the same reason: nothing here
belongs in an order's text, and changing it must not re-run the model over the orders.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from pathlib import Path

log = logging.getLogger(__name__)

SIDECAR = "_staff.json"
VERSION = 1

# How a name came to be here. A person's answer outranks the model's, and the model is
# never allowed to overwrite one -- the shop knows its own staff.
TYPED = "typed"
AI = "ai"

# A name RULED NOT to be the shop's -- a place, a description, the second half of a
# customer's entry. Worth storing for exactly the same reason as a staff name: without it
# the shape test goes on splitting "Anh Tâm - Hà Nội" every time, and the same question
# gets asked of the model on every paste.
NOT_STAFF = "customer"


def fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", (value or "").strip())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.replace("đ", "d").replace("Đ", "D").lower()


def sidecar_path(inbox: Path) -> Path:
    return inbox / SIDECAR


def load(inbox: Path) -> dict[str, str]:
    """name -> how it was learned. Missing or damaged file reads as empty, never raises."""
    path = sidecar_path(inbox)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        names = data.get("names") if isinstance(data, dict) else None
        if not isinstance(names, dict):
            return {}
        allowed = {TYPED, AI, NOT_STAFF}
        return {str(k): (str(v) if str(v) in allowed else TYPED)
                for k, v in names.items() if str(k).strip()}
    except (OSError, ValueError, AttributeError) as exc:
        log.warning("could not read %s (%s) — treating as empty", path.name, exc)
        return {}


def save(inbox: Path, names: dict[str, str]) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    sidecar_path(inbox).write_text(
        json.dumps({"version": VERSION, "names": names}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def record(inbox: Path, name: str, source: str = AI) -> bool:
    """Remember a verdict about `name`. Returns True if it was new.

    A person's answer replaces the model's; the model never replaces a person's, because
    the shop knows its own staff and this is the one thing it cannot be wrong about.
    """
    name = (name or "").strip()
    if not name:
        return False
    names = load(inbox)
    if name in names:
        if source == TYPED and names[name] != TYPED:
            names[name] = TYPED
            save(inbox, names)
        return False
    names[name] = source if source in {TYPED, AI, NOT_STAFF} else AI
    save(inbox, names)
    return True


def forget(inbox: Path, name: str) -> bool:
    """Undo a name. The model can be wrong, and a wrong one splits a customer's name."""
    names = load(inbox)
    if name not in names:
        return False
    names.pop(name)
    save(inbox, names)
    return True


def decided(inbox: Path) -> dict[str, bool]:
    """folded name -> is this the shop's own person.

    Both answers matter. Knowing a name is NOT staff is what stops "Anh Tâm - Hà Nội"
    being split every time it appears.
    """
    return {fold(name): source != NOT_STAFF for name, source in load(inbox).items()}


def folded(inbox: Path) -> set[str]:
    """Just the staff names, folded."""
    return {name for name, is_staff in decided(inbox).items() if is_staff}
