#!/usr/bin/env python3
"""Pull orders out of a Zalo group chat, via the clipboard, into data/inbox/zalo/.

Zalo's export is encrypted and there is no per-conversation export, so the text has to
come out through the UI. Orders live as individual messages inside one busy group chat,
so rather than selecting them one at a time you copy whole chunks and this splits them.

    You:  open the group chat, scroll up, select all, copy. Repeat as you scroll.
    It:   finds the order messages, drops the chatter, filters to one month,
          deduplicates, and writes one .txt per order.

An order is recognised by its header line, which also carries the facts worth having:

    15/8 - đơn 4              -> date + order number
    15/8 đơn 1 - Meloxicam    -> date + order number + customer display name

Overlapping copies are expected and harmless: orders are identified by day + month +
order number, so re-covering ground costs nothing, and an order truncated by where you
stopped selecting is replaced when a later chunk contains more of it.

Text with no order headers falls back to being saved whole, which covers ordinary
conversations.

Nothing is sent anywhere -- this only reads the clipboard and writes local files.

Usage:
    python scripts/zalo_capture.py                    # current month, watch until Ctrl+C
    python scripts/zalo_capture.py --month 7          # July instead
    python scripts/zalo_capture.py --all-months       # no month filter
    python scripts/zalo_capture.py --no-split         # save each copy as one file
    python scripts/zalo_capture.py --debug            # explain what was accepted/rejected
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lavabo.config import Config  # noqa: E402
from lavabo.connectors.zalo_export import DEFAULT_PATTERNS, ORDER_HEADER  # noqa: E402

POLL_SECONDS = 0.5
MIN_TRANSCRIPT_CHARS = 40
MIN_MATCHED_LINES = 2
MIN_CAPTURE_LINES = 3      # unlabelled blocks (order notes) need at least this many lines
INVALID_FILENAME = r'[<>:"/\\|?*\x00-\x1f]'
MAX_NAME_LEN = 80


# --------------------------------------------------------------------- clipboard

def make_clipboard_reader():
    """Return a callable giving current clipboard text, or raise if none available."""
    try:
        import pyperclip

        pyperclip.paste()  # probe: raises on a headless/misconfigured system
        return pyperclip.paste
    except Exception:
        pass

    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()

        def read() -> str:
            try:
                root.update()
                return root.clipboard_get()
            except tkinter.TclError:
                return ""       # clipboard empty or holds non-text

        read()
        return read
    except Exception as exc:
        raise RuntimeError(
            "No clipboard access. Install pyperclip (`pip install pyperclip`), "
            f"or run where a display is available. Underlying error: {exc}"
        ) from None


# ------------------------------------------------------------------------ naming

def parse_senders(text: str) -> Counter[str]:
    """Count sender names using the same patterns the ingest connector uses."""
    patterns = [re.compile(p) for p in DEFAULT_PATTERNS]
    best: Counter[str] = Counter()

    for pattern in patterns:
        counts: Counter[str] = Counter()
        for line in text.splitlines():
            if m := pattern.match(line):
                counts[m.group("name").strip()] += 1
        if sum(counts.values()) > sum(best.values()):
            best = counts

    return best


def first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def derive_name(text: str, own: set[str]) -> tuple[str | None, Counter[str]]:
    """Work out what to call this capture.

    Preference order:
      1. the most frequent sender who isn't us -- works for labelled transcripts
      2. the first line -- for an order note that is the order header
         ("15/8 - don 4"), which identifies the record better than a name would
      3. give up and ask
    """
    senders = parse_senders(text)
    others = Counter({n: c for n, c in senders.items() if n.casefold() not in own})
    if others:
        return others.most_common(1)[0][0], senders

    if head := first_line(text):
        return head[:MAX_NAME_LEN], senders

    return None, senders


def sanitize(name: str) -> str:
    """Make a filename without disfiguring it.

    "15/8 - don 4" has to lose the slash or it becomes a path, but it should stay
    readable as a date: "15-8 - don 4", not "15 - 8 - don 4".
    """
    name = re.sub(INVALID_FILENAME, "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .-")
    return name[:MAX_NAME_LEN] or "unnamed"


@dataclass(slots=True)
class OrderBlock:
    """One order lifted out of a group chat: its header line plus everything under it."""
    header: str
    day: int
    month: int
    year: int | None
    order_no: int
    customer: str | None
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join([self.header, *self.lines]).strip()

    @property
    def key(self) -> tuple[int, int, int]:
        """Business identity of the order: day, month, order number."""
        return (self.day, self.month, self.order_no)

    @property
    def label(self) -> str:
        who = f" - {self.customer}" if self.customer else ""
        return f"{self.day}/{self.month} đơn {self.order_no}{who}"


def split_orders(text: str) -> list[OrderBlock]:
    """Cut a chunk of group chat into order blocks.

    A line matching the order header starts a new block; everything until the next
    header belongs to it. Anything before the first header is chatter and dropped.

    Trailing chatter after an order's last real line is kept rather than guessed at:
    there is no reliable end-of-order marker, and including a stray "ok chị" costs
    nothing at extraction time, whereas trimming too eagerly would lose order lines.
    """
    blocks: list[OrderBlock] = []
    current: OrderBlock | None = None

    for line in text.splitlines():
        if m := ORDER_HEADER.match(line.strip()):
            year = int(m["year"]) if m["year"] else None
            if year is not None and year < 100:
                year += 2000
            current = OrderBlock(
                header=line.strip(),
                day=int(m["day"]),
                month=int(m["month"]),
                year=year,
                order_no=int(m["order"]),
                customer=(m["customer"] or "").strip() or None,
                lines=[],
            )
            blocks.append(current)
        elif current is not None:
            current.lines.append(line.rstrip())

    return blocks


def in_month(block: OrderBlock, month: int, year: int) -> bool:
    """Headers usually omit the year, so an absent one is taken as the target year."""
    return block.month == month and (block.year or year) == year


def existing_orders(inbox: Path) -> dict[tuple[int, int, int], tuple[Path, int]]:
    """Map already-captured order keys to their file and size.

    Keyed on the order itself rather than file content, so re-copying an overlapping
    chunk of the chat does not create duplicates.
    """
    found: dict[tuple[int, int, int], tuple[Path, int]] = {}
    for path in inbox.glob("*.txt"):
        try:
            head = first_line(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if m := ORDER_HEADER.match(head):
            key = (int(m["day"]), int(m["month"]), int(m["order"]))
            found[key] = (path, path.stat().st_size)
    return found


def looks_capturable(text: str) -> bool:
    """Accept transcripts AND unlabelled blocks such as order notes.

    Requiring sender patterns rejected everything that isn't a labelled chat, which
    is most of what this shop actually copies. A multi-line block of real text is
    enough; single stray copies (a URL, a phone number) still fall through.
    """
    if len(text) < MIN_TRANSCRIPT_CHARS:
        return False
    if sum(parse_senders(text).values()) >= MIN_MATCHED_LINES:
        return True
    return len([ln for ln in text.splitlines() if ln.strip()]) >= MIN_CAPTURE_LINES


# ------------------------------------------------------------------------ writing

def unique_path(directory: Path, stem: str) -> Path:
    path = directory / f"{stem}.txt"
    n = 2
    while path.exists():
        path = directory / f"{stem} ({n}).txt"
        n += 1
    return path


def save(text: str, name: str, inbox: Path) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    path = unique_path(inbox, sanitize(name))
    path.write_text(text, encoding="utf-8")
    return path


def existing_hashes(inbox: Path) -> set[str]:
    if not inbox.exists():
        return set()
    return {
        hashlib.sha256(p.read_bytes()).hexdigest()
        for p in inbox.glob("*.txt")
    }


# --------------------------------------------------------------------------- main

def ask_name() -> str:
    try:
        return input("    type the customer name (or Enter to skip): ").strip()
    except EOFError:
        return ""


def handle_orders(text: str, cfg, month: int, year: int, *, all_months: bool) -> tuple[int, int, int]:
    """Split a chat chunk into orders and save the wanted ones.

    Returns (saved, duplicates, out_of_month).
    """
    blocks = split_orders(text)
    if not blocks:
        return (0, 0, 0)

    wanted = blocks if all_months else [b for b in blocks if in_month(b, month, year)]
    skipped_month = len(blocks) - len(wanted)

    known = existing_orders(cfg.zalo.inbox_dir)
    saved = duplicates = 0

    for block in wanted:
        body = block.text
        if block.key in known:
            path, size = known[block.key]
            # A chunk can end mid-order, so a later, longer capture of the same
            # order is a more complete one and replaces the truncated version.
            if len(body.encode("utf-8")) > size:
                path.write_text(body, encoding="utf-8")
                known[block.key] = (path, len(body.encode("utf-8")))
                print(f"  updated {path.name}  (longer capture of the same order)")
                saved += 1
            else:
                duplicates += 1
            continue

        path = save(body, block.header, cfg.zalo.inbox_dir)
        known[block.key] = (path, len(body.encode("utf-8")))
        print(f"  saved   {path.name}  ({len(block.lines)} lines"
              + (f", {block.customer}" if block.customer else "") + ")")
        saved += 1

    bits = [f"{len(blocks)} order(s) in clipboard", f"{saved} saved"]
    if duplicates:
        bits.append(f"{duplicates} already captured")
    if skipped_month:
        bits.append(f"{skipped_month} outside {month:02d}/{year}")
    print("  → " + ", ".join(bits))

    return (saved, duplicates, skipped_month)


def handle(text: str, cfg, own: set[str], forced: str | None) -> Path | None:
    name, senders = derive_name(text, own)

    if forced:
        name = forced
    elif name is None:
        print("  ! could not identify the customer from the copied text.")
        if senders:
            print(f"    senders seen: {', '.join(senders)}")
            print("    (all of them matched zalo.own_names)")
        else:
            print("    no sender lines recognised — this client's copy format differs from")
            print("    the built-in patterns. The text is still saved; the parser gets")
            print("    tuned to it later. Send the file's first ~15 lines to fix parsing.")
        name = ask_name()
        if not name:
            print("    skipped.")
            return None

    path = save(text, name, cfg.zalo.inbox_dir)
    count = sum(senders.values())
    lines = len([ln for ln in text.splitlines() if ln.strip()])
    detail = f"{count} messages" if count else f"{lines} lines, no speaker labels"
    print(f"  saved {path.name}  ({detail}, {len(text):,} chars)")
    return path


def explain_rejection(text: str) -> str:
    """Why looks_capturable() said no — shown in --debug."""
    if len(text) < MIN_TRANSCRIPT_CHARS:
        return f"only {len(text)} chars (need {MIN_TRANSCRIPT_CHARS}+)"
    lines = len([ln for ln in text.splitlines() if ln.strip()])
    return (f"{len(text):,} chars but only {lines} non-empty line(s) and no sender "
            f"patterns (need {MIN_CAPTURE_LINES}+ lines, or {MIN_MATCHED_LINES}+ sender lines)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", type=Path, help="path to config.yaml")
    ap.add_argument("--once", action="store_true", help="exit after one capture")
    ap.add_argument("--name", help="force the customer name for the next capture")
    ap.add_argument("--raw", action="store_true",
                    help="save ANY copied text, even if the format is not recognised "
                         "(asks for the name; use when the built-in patterns don't match "
                         "your Zalo client)")
    ap.add_argument("--debug", action="store_true",
                    help="report every clipboard change and why it was accepted or rejected")
    ap.add_argument("--month", type=int, metavar="M",
                    help="capture orders from this month instead of the current one (1-12)")
    ap.add_argument("--year", type=int, metavar="Y", help="year for --month (default: this year)")
    ap.add_argument("--all-months", action="store_true",
                    help="keep every order found, not just the target month")
    ap.add_argument("--no-split", action="store_true",
                    help="save the whole copied text as one file, without splitting on order headers")
    args = ap.parse_args()

    today = date.today()
    month = args.month or today.month
    year = args.year or today.year

    cfg = Config.load(args.config)
    own = {n.strip().casefold() for n in cfg.zalo.own_names if n.strip()}

    if not own:
        print("WARNING: zalo.own_names is empty in your config, so the script cannot tell")
        print("         your messages from the customer's and will ask for every name.\n")

    try:
        read_clipboard = make_clipboard_reader()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    seen = existing_hashes(cfg.zalo.inbox_dir)
    already = existing_orders(cfg.zalo.inbox_dir)
    scope = "every month" if args.all_months else f"{month:02d}/{year} only"

    print(f"Watching clipboard -> {cfg.zalo.inbox_dir}")
    print(f"({len(already)} order(s) already captured; keeping {scope})"
          + ("  [RAW]" if args.raw else "") + ("  [DEBUG]" if args.debug else "")
          + ("  [NO-SPLIT]" if args.no_split else "") + "\n")
    print("In Zalo: open the group chat, scroll up through the month you want, then")
    print("         select all and copy (Cmd+A/Cmd+C on macOS, Ctrl+A/Ctrl+C on Windows).")
    print("         Copy in chunks as you scroll — overlapping chunks are fine, each order")
    print("         is saved once. Ctrl+C here to stop — always Ctrl.\n")
    print("Only messages starting with an order header are kept, e.g.")
    print("    15/8 - đơn 4            or    15/8 đơn 1 - Meloxicam")
    print("Everything else in the chat is ignored.\n")

    last = read_clipboard() or ""
    captured = 0

    try:
        while True:
            time.sleep(POLL_SECONDS)
            current = read_clipboard() or ""

            if current == last or not current:
                continue
            last = current

            recognised = looks_capturable(current)

            if args.debug:
                preview = current.splitlines()[0][:70] if current.splitlines() else ""
                verdict = "accepted" if recognised else f"rejected: {explain_rejection(current)}"
                print(f"  [debug] clipboard changed — {verdict}")
                print(f"  [debug] first line: {preview!r}")

            if not recognised and not args.raw:
                continue

            digest = hashlib.sha256(current.encode("utf-8")).hexdigest()
            if digest in seen:
                print("  (already captured — skipping duplicate)")
                continue

            if not args.no_split and any(ORDER_HEADER.match(ln.strip()) for ln in current.splitlines()):
                saved, _, _ = handle_orders(current, cfg, month, year,
                                            all_months=args.all_months)
                seen.add(digest)
                captured += saved
                if args.once and saved:
                    break
                continue

            if handle(current, cfg, own, args.name):
                seen.add(digest)
                captured += 1
                args.name = None       # a forced name applies once only
                if args.once:
                    break

    except KeyboardInterrupt:
        print()

    print(f"\n{captured} order(s)/conversation(s) captured. "
          "Next: lavabo ingest --source zalo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
