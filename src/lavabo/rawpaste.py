"""Every pasted chunk of chat, stored verbatim before anything tries to understand it.

The scroll-and-copy in Zalo is the one step in this whole pipeline a human cannot cheaply
repeat: Zalo lazy-loads history, so recovering a month means scrolling it again by hand.
Everything downstream -- splitting into orders, extraction, the workbook -- is derived and
can be recomputed from the text at any time. So the text is written to disk first, before
any parsing, and kept.

That ordering is what makes the segmentation step safe to move from regex to a model. A
model call can fail -- quota, rate limit, expired key, an outage at the provider -- and if
it fails while holding the only copy of the paste, the cost of that failure is landed on
the person standing there with a phone, who has to go and scroll Zalo again. With the raw
text already on disk, the same failure costs a retry over stored input, exactly as a failed
extraction does today.

Stored OUTSIDE the Zalo inbox on purpose: the connector's _files() walks the inbox with
rglob, so anything left under it is read as a transcript and extracted. These are inputs to
capture, not conversations.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

INDEX = "index.json"
VERSION = 1

# Pastes below this are not worth keeping: a stray copy of a single line, a click that
# selected one message. The capture path already ignores them.
MIN_CHARS = 40

# Kept per month-year being captured, newest first. A month is captured in overlapping
# chunks -- deliberately, that is the documented workflow -- so a busy month can be a few
# dozen pastes. This bounds the store without ever pruning the current month's work.
KEEP_PER_PERIOD = 400


def store_dir(inbox: Path) -> Path:
    """Sibling of the inbox, not a child of it. See the module docstring."""
    return inbox.parent / "raw" / inbox.name


def load_index(inbox: Path) -> list[dict[str, Any]]:
    """Newest first. A damaged index reads as empty rather than stopping a capture.

    The .txt files are the irreplaceable part; the index is bookkeeping over them and can
    be rebuilt by reading the directory, so it is never worth failing a paste over.
    """
    path = store_dir(inbox) / INDEX
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("pastes") if isinstance(data, dict) else None
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    except (OSError, ValueError, AttributeError) as exc:
        log.warning("could not read %s (%s) — treating as empty", path, exc)
        return []


def save_index(inbox: Path, entries: list[dict[str, Any]]) -> None:
    directory = store_dir(inbox)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / INDEX).write_text(
        json.dumps({"version": VERSION, "pastes": entries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def store(inbox: Path, text: str, *, month: int, year: int,
          closer: str | None = None) -> Path | None:
    """Write one paste verbatim. Returns its path, or None if there was nothing to keep.

    Idempotent on content: re-pasting the same chunk -- the normal way this shop captures,
    in overlapping sweeps -- refreshes the existing entry rather than writing a second copy
    of the same text. Safe to call from more than one place in a single capture for the
    same reason.
    """
    text = text or ""
    if len(text.strip()) < MIN_CHARS:
        return None

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    directory = store_dir(inbox)
    entries = load_index(inbox)

    for entry in entries:
        if entry.get("sha256") == digest:
            path = directory / str(entry.get("file", ""))
            if path.exists():
                entry["last_seen_at"] = _now()
                entry["seen"] = int(entry.get("seen") or 1) + 1
                save_index(inbox, entries)
                return path
            break                                  # indexed but gone: fall through, rewrite

    name = f"{datetime.now():%Y%m%d-%H%M%S}-{digest[:8]}.txt"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")

    entries = [e for e in entries if e.get("sha256") != digest]
    entries.insert(0, {
        "file": name,
        "sha256": digest,
        "captured_at": _now(),
        "last_seen_at": _now(),
        "seen": 1,
        "chars": len(text),
        "month": month,
        "year": year,
        "closer": closer or None,
        # How this paste was turned into orders. The regex splitter is the only segmenter
        # today; an AI pass records itself here so a resegment can find what predates it.
        "segmenter": "regex",
    })
    _prune(directory, entries)
    save_index(inbox, entries)
    return path


def _prune(directory: Path, entries: list[dict[str, Any]]) -> None:
    """Drop the oldest pastes of each period past KEEP_PER_PERIOD, in place.

    Per period rather than overall so that capturing a heavy month cannot evict a lighter
    one that is still being worked on.
    """
    kept: dict[tuple, int] = {}
    survivors: list[dict[str, Any]] = []
    for entry in entries:
        period = (entry.get("year"), entry.get("month"))
        kept[period] = count = kept.get(period, 0) + 1
        if count > KEEP_PER_PERIOD:
            try:
                (directory / str(entry.get("file", ""))).unlink(missing_ok=True)
            except OSError as exc:
                log.warning("could not prune %s (%s)", entry.get("file"), exc)
                survivors.append(entry)
            continue
        survivors.append(entry)
    entries[:] = survivors


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def text_of(inbox: Path, entry: dict[str, Any]) -> str | None:
    """The stored text for one index entry, or None if the file has gone."""
    path = store_dir(inbox) / str(entry.get("file") or "")
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None
