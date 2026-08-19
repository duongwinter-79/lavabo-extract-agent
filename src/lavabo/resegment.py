"""Re-running capture over pastes already on disk, after the capture code changes.

The extraction step already handles its own version of this: the cache is keyed on the
prompt version, the schema fingerprint and the model, so improving a prompt correctly
re-extracts everything. Capture had no equivalent, and it needed one for the same reason.

An order's .txt is the output of whatever splitting and trimming code existed the day it
was captured. Fix a header pattern that was dropping orders, or a trim that was keeping
chatter, and the orders already on disk stay as the old code left them. Pasting the same
text again does not fix it either: the merge rule prefers the LONGER body, which is right
when a scroll was cut short and wrong when the old code over-captured, so a correction that
makes an order shorter is discarded as a duplicate.

This replays the stored pastes through today's code and reconciles the result.

**It never deletes an order it cannot reproduce.** Orders captured before pastes were kept,
or whose paste has been pruned, have no source to replay -- so they are left exactly as they
are and reported, rather than being quietly lost to a maintenance command.

**It never overwrites a closer.** Người chốt đơn is typed by a person and feeds the shop's
revenue split; the replay may know an older answer than the one on disk. New orders get the
closer recorded with their paste, existing ones keep what they have.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import closers, extras, flags, rawpaste

log = logging.getLogger(__name__)


@dataclass
class Change:
    filename: str
    kind: str            # added | changed | unreproducible
    detail: str = ""

    def __str__(self) -> str:
        return f"[{self.kind}] {self.filename}" + (f" — {self.detail}" if self.detail else "")


@dataclass
class Result:
    pastes: int = 0
    rebuilt: int = 0
    changes: list[Change] = field(default_factory=list)
    applied: bool = False

    def of(self, kind: str) -> list[Change]:
        return [c for c in self.changes if c.kind == kind]

    def summary(self) -> str:
        added = len(self.of("added"))
        changed = len(self.of("changed"))
        stale = len(self.of("unreproducible"))
        verb = "applied" if self.applied else "would change"
        bits = [f"replayed {self.pastes} paste(s) -> {self.rebuilt} order(s)",
                f"{verb}: {added} new, {changed} corrected"]
        if stale:
            bits.append(f"{stale} order(s) have no stored paste and were left alone")
        return "; ".join(bits)


def _capture_into(inbox: Path, pastes: list[dict[str, Any]], source_inbox: Path) -> None:
    """Replay pastes into an empty inbox, oldest first.

    Oldest first because the merge rules are order-dependent -- a fuller capture replaces a
    shorter one, a differing one becomes a version -- so replaying in capture order is what
    reproduces the state today's code would have reached.
    """
    import io
    import sys
    from contextlib import redirect_stdout

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import zalo_capture as zc                                   # noqa: E402

    from .config import Config

    cfg = Config()
    cfg.zalo.inbox_dir = inbox
    # Swallowed on purpose. The replay narrates itself as "saved 2 orders", which is true
    # of the staging directory and false of anything the operator can see -- and a dry run
    # that prints "saved" is worse than one that prints nothing.
    with io.StringIO() as sink, redirect_stdout(sink):
        for entry in reversed(pastes):                          # index is newest first
            text = rawpaste.text_of(source_inbox, entry)
            if not text:
                continue
            zc.handle_orders(text, cfg, int(entry.get("month") or 0),
                             int(entry.get("year") or 0), all_months=True, trim=True,
                             closer=entry.get("closer"), store_raw=False)


def run(cfg, *, month: int | None = None, year: int | None = None,
        apply: bool = False) -> Result:
    """Replay stored pastes through today's capture code and reconcile.

    With apply=False nothing is written -- the point is to see what a code change would do
    to orders already captured before doing it.
    """
    inbox = cfg.zalo.inbox_dir
    pastes = [p for p in rawpaste.load_index(inbox)
              if (month is None or int(p.get("month") or 0) == month)
              and (year is None or int(p.get("year") or 0) == year)]
    result = Result(pastes=len(pastes), applied=apply)
    if not pastes:
        return result

    with tempfile.TemporaryDirectory(prefix="lavabo-resegment-") as tmp:
        staging = Path(tmp) / "zalo"
        staging.mkdir(parents=True)
        _capture_into(staging, pastes, inbox)

        rebuilt = {p.name: p.read_text(encoding="utf-8") for p in staging.glob("*.txt")}
        result.rebuilt = len(rebuilt)
        live = {p.name: p.read_text(encoding="utf-8") for p in inbox.glob("*.txt")}

        for name, body in sorted(rebuilt.items()):
            if name not in live:
                result.changes.append(Change(name, "added", "today's code finds this order"))
            elif live[name].strip() != body.strip():
                before, after = len(live[name].splitlines()), len(body.splitlines())
                result.changes.append(
                    Change(name, "changed", f"{before} lines -> {after}"))
        for name in sorted(live.keys() - rebuilt.keys()):
            result.changes.append(
                Change(name, "unreproducible", "no stored paste produces it — left alone"))

        if apply and (result.of("added") or result.of("changed")):
            _apply(inbox, staging, rebuilt, live)
    return result


def _apply(inbox: Path, staging: Path, rebuilt: dict[str, str], live: dict[str, str]) -> None:
    """Write the replayed orders over the live ones, merging the sidecars.

    Sidecars are merged rather than replaced. extras and flags are additive and both
    deduplicate, so a union loses nothing. Closers are the exception in the other
    direction: an existing one is kept, because the person who typed it knows better than
    a replay of an older paste.
    """
    for name, body in rebuilt.items():
        if name not in live or live[name].strip() != body.strip():
            (inbox / name).write_text(body, encoding="utf-8")

    staged_extras = extras.load(staging)
    for filename, items in staged_extras.items():
        for item in items:
            extras.record(inbox, filename, item.get("kind", "update"), item["text"],
                          item.get("confidence", "high"))

    staged_flags = flags.load(staging)
    for filename, marks in staged_flags.items():
        for mark in marks:
            flags.record(inbox, filename, mark)

    known = closers.load(inbox)
    for filename, name in closers.load(staging).items():
        if not known.get(filename):
            closers.record(inbox, filename, name)


def backup(inbox: Path) -> Path:
    """Copy the inbox aside before applying. Cheap, and these are text files."""
    from datetime import datetime

    target = inbox.parent / f"{inbox.name}-backup-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copytree(inbox, target)
    return target
