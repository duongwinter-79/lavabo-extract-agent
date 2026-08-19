#!/usr/bin/env python3
"""Pull orders out of a Zalo group chat, via the clipboard, into data/inbox/zalo/.

Zalo's export is encrypted and there is no per-conversation export, so the text has to
come out through the UI. Orders live as individual messages inside one busy group chat,
so rather than selecting them one at a time you copy whole chunks and this splits them.

    You:  open the group chat, scroll up, select all, copy. Repeat as you scroll.
    It:   finds the order messages, drops the chatter, filters to one month,
          deduplicates, and writes one .txt per order.

An order is recognised by its header line, which also carries the facts worth having:

    15/8 - đơn 4                 -> date + order number
    15/8 đơn 1 - Meloxicam       -> date + order number + customer display name
    2/7 đơn 2 (Trần Thị Liên)    -> the same, with the name in brackets

Overlapping copies are expected and harmless: orders are identified by day + month +
order number, so re-covering ground costs nothing, and an order truncated by where you
stopped selecting is replaced when a later chunk contains more of it.

Each order is cut at its deposit line ("Đã cọc 500k", "Đã cọc 1tr, còn 19tr2"), since
everything after that is other people talking and senders cannot be told apart. A
trailing "Note:" line is kept. --no-trim disables this.

Text with no order headers falls back to being saved whole, which covers ordinary
conversations.

Nothing is sent anywhere -- this only reads the clipboard and writes local files.

Usage:
    python scripts/zalo_capture.py                    # current month, watch until Ctrl+C
    python scripts/zalo_capture.py --month 7          # July instead
    python scripts/zalo_capture.py --all-months       # no month filter
    python scripts/zalo_capture.py --no-split         # save each copy as one file
    python scripts/zalo_capture.py --debug            # explain what was accepted/rejected
    python scripts/zalo_capture.py --retrim           # trim files already captured, in place
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import NamedTuple
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lavabo import closers, extras, flags, rawpaste, segment  # noqa: E402
from lavabo.config import Config  # noqa: E402
from lavabo.connectors.zalo_export import (  # noqa: E402
    DEFAULT_PATTERNS, ORDER_HEADER, header_customer)  # noqa: E402

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
    # True when day/month were exchanged by resolve_swapped_dates -- see split_orders.
    # original_header is always the literal line as typed, for reporting the swap;
    # header is what actually gets saved and later re-parsed at ingest, so it must
    # carry the corrected date for the fix to survive past this capture session.
    date_swapped: bool = False
    original_header: str = ""
    # Later messages the segmenter attributed to THIS order, as (text, confidence).
    # Empty for regex-produced blocks, whose revisions come from trailing_update instead.
    ai_updates: list[tuple[str, str]] = field(default_factory=list)

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


def fold(text: str) -> str:
    """Lowercase and strip Vietnamese diacritics, so typo-tolerant matching is possible.

    Shop staff type fast and often skip tone conversion: "Tổng" arrives as "Toongr"
    (telex, where oo->ô and r->hook). Folding both sides to plain ASCII means one
    pattern covers "Tổng", "Tong", "Toongr" and "TỔNG" without enumerating variants.
    """
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.replace("đ", "d").replace("Đ", "D").lower()


# Matched against the FOLDED line, and searched anywhere in it rather than anchored:
# total and deposit are frequently on one line -- "Toongr 6tr, đã cọc 500k".
# A number must follow within a few characters, so chatter like "em cọc rồi ạ" (no
# amount) cannot be mistaken for the real deposit.
#
# fold() strips every combining mark, so "cọc" (stake/deposit) and "cộc" (short/stubby,
# as in "gương cộc" -- a cropped mirror style) become the identical string "coc". Product
# lines naming that style are then followed by a dimension ("gương cộc - 80x40"), which
# supplies the trailing digit DEPOSIT_ANY looks for -- so the FIRST line of the order was
# being mistaken for its deposit line, and _terminator (first match wins) discarded
# everything genuine after it: the real total, deposit, phone and address. Excluding
# "gương " immediately before is exact for this collision without narrowing anything a
# real deposit line relies on -- every deposit in this shop's own messages is written
# "(đã/đaz) cọc ...", never preceded by "gương".
DEPOSIT_ANY = re.compile(r"\b(?:da\s+)?(?<!guong )coc\b[^\d\n]{0,12}\d")
# "tong", "toong", "toongr", "tongr" ... o repeated, optional trailing telex tone key.
TOTAL_ANY = re.compile(r"\bto+ng[rsfjx]?\b[^\d\n]{0,12}\d")
# A line addressed at someone is group chatter, never part of an order.
MENTION_LINE = re.compile(r"^\s*@")
# Lines that still belong to the order even though they follow the terminator.
TRAILING_KEEP = re.compile(r"^\s*(?:note|ghi\s*ch[uu])\b\s*[:\-]?")


def _terminator(lines: list[str]) -> int | None:
    """Index of the last line belonging to the order, or None if undecidable."""
    folded = [fold(ln) for ln in lines]

    for i, ln in enumerate(folded):
        if DEPOSIT_ANY.search(ln):
            return i
    for i, ln in enumerate(folded):
        if TOTAL_ANY.search(ln):
            return i
    # No money line found. An @mention is still a reliable start-of-chatter marker,
    # so end the order on the line before the first one.
    for i, ln in enumerate(lines):
        if MENTION_LINE.match(ln):
            return i - 1 if i else None
    return None


def trim_after_deposit(lines: list[str]) -> tuple[list[str], int]:
    """Cut an order block at its deposit line. Returns (kept, dropped_count).

    Takes the FIRST money match, not the last: the real one is written by the shop as
    part of the order, while anything later is chatter.

    A "Note:" line immediately following is kept -- it is part of the order and feeds
    the note column. When nothing identifies an end the block is left untouched, since
    guessing risks discarding real order lines.
    """
    end = _terminator(lines)
    if end is None:
        return lines, 0

    kept = lines[: end + 1]
    for ln in lines[end + 1:]:
        if not ln.strip():
            continue
        if TRAILING_KEEP.match(fold(ln)):
            kept.append(ln)
        else:
            break

    # Counted by difference rather than by slice position, since blank lines skipped
    # while scanning for trailing keepers would misalign an index-based count.
    dropped = len([ln for ln in lines if ln.strip()]) - len([ln for ln in kept if ln.strip()])
    return kept, dropped


# Words that mark a trailing message as changing the order rather than chatting about it.
# Matched against the FOLDED line, so "đổi"/"doi" and "Thu thêm"/"thu them" both hit.
UPDATE_WORDS = re.compile(
    r"\b(?:them|lay\s+them|thu\s+them|tong\s+thu\s+ho|thay\s+doi|doi\s+.*\s*thanh|"
    r"doi\s+tu|sua\s+lai|bo\s+.*\s+di|giam\s+cho\s+khach)\b"
)


def trailing_update(lines: list[str], kept: list[str]) -> str:
    """The part trim_after_deposit cut, when it reads as a change to the order.

    Everything after an order's deposit line is normally other people talking, which is
    why it is cut. But the shop also revises orders there, with no new header --
    "Đơn này lấy thêm / 1 tủ M52 + gương gấu / Tổng thu hộ 13.800". Discarding that is
    how Thảo Nguyễn exported at 6.000.000 against a real 14.000.000.

    Requires both an update word and a digit somewhere in the remainder: "Thêm 1 cây sen
    / Thu thêm 2.900" qualifies, while "đơn này đi chưa ạ?" does not. Returns "" when the
    remainder is ordinary chatter, which is the overwhelmingly common case.
    """
    remainder = [ln for ln in lines[len(kept):] if ln.strip()]
    if not remainder:
        return ""
    # Stop at the first @mention: past that it is unambiguously group conversation, and
    # a revision written before one should not drag the whole thread in with it.
    cut = next((i for i, ln in enumerate(remainder) if MENTION_LINE.match(ln)), len(remainder))
    remainder = remainder[:cut]
    if not remainder:
        return ""

    blob = fold(" ".join(remainder))
    if not UPDATE_WORDS.search(blob) or not re.search(r"\d", blob):
        return ""
    return "\n".join(remainder).strip()


def _swap_header_date(line: str, match: re.Match) -> str:
    """Exchange the day/month substrings in a header line, touching nothing else.

    Slices around the matched spans rather than reformatting, so a leading zero or an
    unusual separator survives exactly as typed -- only the two numbers trade places.
    """
    day_s, month_s = match["day"], match["month"]
    return (line[: match.start("day")] + month_s
            + line[match.end("day"): match.start("month")] + day_s
            + line[match.end("month"):])


def _should_swap(block: "OrderBlock", prev: "OrderBlock | None", next_: "OrderBlock | None",
                 target_month: int) -> bool:
    """Should this block's day/month be exchanged?

    Deliberately narrow, on purpose: swapping must land EXACTLY on the month being
    captured -- not merely close to it -- and at least one immediate neighbour in the
    pasted text must already, literally, be that same month. A block that just belongs
    to a different month (normal in a paste covering several months at once, which
    this shop routinely sends) has no target-month neighbour and fails this check, so
    it is left for the in-month filter downstream to exclude as intended, rather than
    being forced to fit a month it was never written for.
    """
    if block.month == target_month or block.day > 12 or block.day == block.month:
        return False
    if block.day != target_month:
        return False
    return (prev is not None and prev.month == target_month) or \
           (next_ is not None and next_.month == target_month)


def resolve_swapped_dates(blocks: list["OrderBlock"], target_month: int) -> None:
    """Correct a transposed day/month in place, using each block's position in the
    pasted text as the only evidence beyond the digits themselves.

    Staff write headers like "8/3 đơn 1" by hand, always as day/month -- but transpose
    the two often enough that it shows up in real capture sessions ("8/3" meant as
    3 August, typed day-first out of habit). The header alone cannot decide this: both
    readings are valid dates. What decides it is context -- the surrounding orders were
    typed by the same person in the same sitting, and are overwhelmingly one month. See
    _should_swap for the exact, conservative rule.

    Runs left to right, so a corrected block can corroborate the next one in a run of
    several typos in a row -- the real case behind this was a single mistyped header
    sitting between an already-correct order on each side, but nothing here assumes
    only one exists.

    Known limitation, accepted rather than engineered around: `target_month` is trusted
    as ground truth for what "already correct" means. If it is chosen far from what the
    paste actually contains -- capturing March while pasting a chat that is almost
    entirely August -- an unrelated, genuinely-correct date can be miscorroborated by
    the very typo this exists to fix. That does not happen in normal use, where the
    operator selects the month most of what they are pasting belongs to; every realistic
    target checked against this shop's own multi-month chat log left every unambiguous
    date untouched.
    """
    for i, block in enumerate(blocks):
        prev = blocks[i - 1] if i > 0 else None
        next_ = blocks[i + 1] if i + 1 < len(blocks) else None
        if not _should_swap(block, prev, next_, target_month):
            continue
        match = ORDER_HEADER.match(block.header)
        block.header = _swap_header_date(block.header, match)
        block.day, block.month = block.month, block.day
        block.date_swapped = True


def split_orders(text: str, target_month: int | None = None) -> list[OrderBlock]:
    """Cut a chunk of group chat into order blocks.

    A line matching the order header starts a new block; everything until the next
    header belongs to it. Anything before the first header is chatter and dropped.

    Trailing chatter after an order's last real line is kept rather than guessed at:
    there is no reliable end-of-order marker, and including a stray "ok chị" costs
    nothing at extraction time, whereas trimming too eagerly would lose order lines.

    `target_month` resolves a transposed day/month against the month being captured
    (see resolve_swapped_dates); omit it to keep every header exactly as written.
    """
    blocks: list[OrderBlock] = []
    current: OrderBlock | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if m := ORDER_HEADER.match(stripped):
            year = int(m["year"]) if m["year"] else None
            if year is not None and year < 100:
                year += 2000
            current = OrderBlock(
                header=stripped,
                day=int(m["day"]),
                month=int(m["month"]),
                year=year,
                order_no=int(m["order"]),
                customer=header_customer(m) or None,
                lines=[],
                original_header=stripped,
            )
            blocks.append(current)
        elif current is not None:
            current.lines.append(line.rstrip())

    if target_month is not None:
        resolve_swapped_dates(blocks, target_month)

    return blocks


def blocks_from_segments(result, target_month: int | None = None) -> list[OrderBlock]:
    """Model segmentation -> the same OrderBlock the regexes produce.

    Converting rather than introducing a parallel type is the whole safety argument for
    this change: dedup, the month filter, the closer sidecar, trimming and saving all keep
    running on exactly the objects they already run on, so switching segmenter changes
    where blocks come from and nothing about what happens to them. The order key stays
    (day, month, số đơn) computed from integers, so re-pasting an overlapping chunk still
    lands on the same file.

    The model's own date_swapped is honoured, but resolve_swapped_dates still runs when a
    target month is given: it is cheap, it is deterministic, and an order it would fix
    that the model left alone is a fix either way.
    """
    blocks: list[OrderBlock] = []
    for order in result.orders:
        body = (order.body or "").strip()
        lines = body.splitlines()
        # The model is asked for the body with its header included; tolerate either, so a
        # response that omits the header line does not lose the whole first product line.
        if lines and lines[0].strip() == order.header.strip():
            lines = lines[1:]
        blocks.append(OrderBlock(
            header=order.header,
            day=order.day,
            month=order.month,
            year=None,                     # headers carry no year; in_month supplies it
            order_no=order.order_number,
            customer=order.customer,
            lines=[ln.rstrip() for ln in lines],
            date_swapped=order.date_swapped,
            original_header=order.header,
            ai_updates=[(u.text, u.confidence) for u in order.updates],
        ))
    if target_month is not None:
        resolve_swapped_dates(blocks, target_month)
    return blocks


def in_month(block: OrderBlock, month: int, year: int) -> bool:
    """Headers usually omit the year, so an absent one is taken as the target year."""
    return block.month == month and (block.year or year) == year


def merge_into(inbox: Path, path: Path, existing: str, block: "OrderBlock") -> str:
    """Fold a re-seen order into the file already holding it, without losing text.

    An order header can turn up more than once for two unrelated reasons, and telling
    them apart matters:

    1. **The same message, captured again** — overlapping pastes, or a chunk that ended
       mid-order and was re-copied in full. One body contains the other, so the longer
       one is genuinely the more complete capture and wins.
    2. **A different message about the same order** — "13/7 đơn 5 đã giao" arriving days
       after "13/7 đơn 5" itself. Neither is more complete; they are both true.

    Comparing sizes alone cannot distinguish these, and treating (2) as (1) silently
    replaced a real order with a follow-up fragment whenever the fragment happened to be
    the larger of the two. Early orders suffer most, because they accumulate the most
    follow-ups.

    (2) used to be appended into the same file. That lost nothing, but it produced one
    order carrying two contradictory totals -- "Tổng 5.800" and "Tổng 12.000" in the same
    block -- and left the model to pick, on the one field where a wrong answer moves
    money. It is now kept as a separate version instead (see extras.py), so both are
    preserved and the choice reaches a human.
    """
    body = block.text.strip()
    if body in existing:                       # already have every line of it
        return "duplicate"
    if existing in body:                       # a fuller capture of the same message
        path.write_text(body, encoding="utf-8")
        return "updated"

    # Same order key, materially different text: a revision, not a fuller capture.
    if extras.record(inbox, path.name, "version", body):
        return "version"
    return "duplicate"


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


def order_gaps(inbox: Path, month: int) -> list[str]:
    """Days in `month` whose captured order numbers skip one -- the strongest signal
    available that a Ctrl+A/Ctrl+C did not reach the whole conversation.

    Zalo PC keeps only a scrollable window of messages actually loaded, so a single
    select-all can silently miss part of the month no matter how far you scrolled --
    this is the practical size limit, not the clipboard, which has no real cap of its
    own. Capturing in overlapping chunks as you scroll is the standing workaround (safe
    because orders dedupe by day/month/order number), but nothing short of comparing
    against a source of truth can PROVE nothing was missed.

    This is the closest thing to one that costs nothing extra: within a shop day, order
    numbers are written 1, 2, 3... with no gaps -- staff number them by hand as they go.
    A day with đơn 2, 3, 4 captured but no đơn 1 almost certainly has an order sitting
    further up in Zalo that was never pasted. A day with zero captured orders is not
    flagged; the shop does not get orders every single day, and that is not evidence of
    anything missing.
    """
    by_day: dict[int, set[int]] = {}
    for day, m, order_no in existing_orders(inbox):
        if m == month:
            by_day.setdefault(day, set()).add(order_no)

    notes = []
    for day in sorted(by_day):
        nums = by_day[day]
        missing = [n for n in range(1, max(nums)) if n not in nums]
        if missing:
            have = ", ".join(str(n) for n in sorted(nums))
            miss = ", ".join(str(n) for n in missing)
            notes.append(f"{day}/{month}: thiếu đơn {miss} (đã có đơn {have})")
    return notes


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


class CaptureResult(NamedTuple):
    """What one paste did. A named tuple rather than a bare one because this has grown
    a field twice now, and every growth silently broke a caller that unpacked by arity."""
    saved: int
    duplicates: int
    out_of_month: int
    date_swaps: list[str]
    # Counted apart because they mean different things to the arithmetic: a version is
    # its own block in the paste, so saved + duplicates + out_of_month + versions equals
    # the number of orders found. An update is trailing text inside a block already
    # counted under saved, so adding it in would double-count that order.
    versions: int
    updates: int


def handle_orders(text: str, cfg, month: int, year: int, *,
                  all_months: bool, trim: bool = True,
                  closer: str | None = None) -> CaptureResult:
    """Split a chat chunk into orders and save the wanted ones.

    `closer` is who chốt these orders. Recorded per order in a sidecar rather than in
    the note itself, and applied to duplicates too, so re-pasting with the right name
    corrects orders captured earlier under the wrong one.

    Returns (saved, duplicates, out_of_month, date_swaps). date_swaps is one string per
    order whose day/month were exchanged against `month` -- see resolve_swapped_dates --
    since that correction has to happen before the in-month filter below (an unswapped
    "8/3" during an August capture is excluded here as "outside 08/2026" and never even
    reaches disk) and is otherwise invisible: it changes what gets filed, silently.
    """
    # Before anything tries to understand the text. Splitting, extraction and the
    # workbook are all derived and can be recomputed; the paste itself cannot, short of
    # scrolling Zalo again by hand. Stored even when no order is recognised, since "the
    # splitter found nothing" is exactly the case a better segmenter would revisit.
    rawpaste.store(cfg.zalo.inbox_dir, text, month=month, year=year, closer=closer)

    blocks = split_orders(text, target_month=month)
    mode = getattr(cfg.extract, "ai_segmentation", "off")
    fallback = False

    if mode in ("shadow", "on"):
        ai = segment.run(cfg, text, month, year)
        findings = segment.compare(ai, blocks) if ai is not None else []
        if ai is not None:
            lines = [segment.summarise(ai, blocks, findings)] + [f"  {d}" for d in findings]
            segment.log_shadow(cfg.zalo.inbox_dir, lines)
            for line in lines:
                print(f"  {line}")
        if mode == "on":
            # The model decides only when it actually answered. A failed call, a missing
            # key, an empty answer -- any of these fall back to the regexes rather than
            # losing the paste, and the orders that came out of the fallback are flagged
            # so nobody has to remember which ones missed the better segmenter.
            if ai is not None and ai.ok and ai.orders:
                blocks = blocks_from_segments(ai, target_month=month)
            else:
                fallback = True
                print("  segmentation unavailable — dùng regex, đánh dấu "
                      f"{flags.NO_AI!r}")

    if not blocks:
        return CaptureResult(0, 0, 0, [], 0, 0)

    wanted = blocks if all_months else [b for b in blocks if in_month(b, month, year)]
    skipped_month = len(blocks) - len(wanted)
    date_swaps = [f"{b.original_header} → {b.day}/{b.month}" for b in blocks if b.date_swapped]
    for note in date_swaps:
        print(f"  note    ngày/tháng bị đảo: {note}")

    known = existing_orders(cfg.zalo.inbox_dir)
    saved = duplicates = trimmed_lines = versions = updates = 0

    inbox = cfg.zalo.inbox_dir

    def note_order(path: Path, revisions: list[tuple[str, str]]) -> int:
        """Record everything held beside an order rather than inside it. Returns how
        many revisions were new, so the counts stay honest across both save paths."""
        closers.record(inbox, path.name, closer)
        if fallback:
            flags.record(inbox, path.name, flags.NO_AI)
        elif mode == "on":
            # A successful re-paste upgrades an order the fallback had captured: the
            # better segmenter has now seen it, so the warning no longer applies.
            flags.clear(inbox, path.name, flags.NO_AI)
        return sum(1 for text, confidence in revisions
                   if extras.record(inbox, path.name, "update", text, confidence))

    for block in wanted:
        update = ""
        if trim:
            kept, dropped = trim_after_deposit(block.lines)
            # Read the discarded tail BEFORE replacing block.lines, since the whole
            # point is to recover what the trim was about to throw away.
            update = trailing_update(block.lines, kept)
            block.lines = kept
            trimmed_lines += dropped
        # Both sources feed the same sidecar, which dedups on text: the trimmed tail from
        # the regex path, and whatever the segmenter attributed to this order. In "on"
        # mode the second is the real one; keeping the first costs nothing and covers a
        # model that returned a body with the revision still attached to it.
        revisions = ([(update, "high")] if update else []) + list(block.ai_updates)
        body = block.text
        if block.key in known:
            path, _ = known[block.key]
            existing = path.read_text(encoding="utf-8", errors="replace").strip()
            action = merge_into(inbox, path, existing, block)
            if action == "duplicate":
                duplicates += 1
            elif action == "version":
                # A competing version of an order already captured: stored beside it,
                # not merged in, and surfaced for review rather than counted as saved.
                versions += 1
                print(f"  version {path.name}  (bản khác — cần xem lại)")
            else:
                known[block.key] = (path, path.stat().st_size)
                print(f"  {action:7} {path.name}")
                saved += 1
            updates += note_order(path, revisions)
            continue

        path = save(body, block.header, inbox)
        updates += note_order(path, revisions)
        known[block.key] = (path, len(body.encode("utf-8")))
        print(f"  saved   {path.name}  ({len(block.lines)} lines"
              + (f", {block.customer}" if block.customer else "") + ")")
        saved += 1

    bits = [f"{len(blocks)} order(s) in clipboard", f"{saved} saved"]
    if duplicates:
        bits.append(f"{duplicates} already captured")
    if skipped_month:
        bits.append(f"{skipped_month} outside {month:02d}/{year}")
    if versions:
        bits.append(f"{versions} bản khác")
    if updates:
        bits.append(f"{updates} có bổ sung")
    if trimmed_lines:
        bits.append(f"{trimmed_lines} trailing line(s) trimmed")
    print("  → " + ", ".join(bits))

    return CaptureResult(saved, duplicates, skipped_month, date_swaps,
                         versions, updates)


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


def retrim_existing(inbox: Path) -> int:
    """Re-trim files already in the inbox, in place.

    Needed because captures taken before trimming existed still carry trailing
    chatter, and re-capturing will not fix them: a trimmed block is *shorter* than
    the stored one, and the dedupe rule only replaces a file when the new capture is
    longer. This applies the trim directly instead.
    """
    changed = 0
    for path in sorted(inbox.glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if not lines:
            continue

        header, body = lines[0], lines[1:]
        if not ORDER_HEADER.match(header.strip()):
            continue                       # not an order note; leave it alone

        kept, dropped = trim_after_deposit(body)
        if not dropped:
            continue

        path.write_text("\n".join([header, *kept]).strip(), encoding="utf-8")
        print(f"  trimmed {path.name}  ({dropped} trailing line(s) removed)")
        changed += 1

    print(f"\n{changed} file(s) trimmed." if changed else "\nNothing to trim.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", type=Path, help="path to config.yaml")
    ap.add_argument("--once", action="store_true", help="exit after one capture")
    ap.add_argument("--name", help="force the customer name for the next capture")
    ap.add_argument("--closer", metavar="NAME",
                    help="who chốt the orders in this capture; recorded per order "
                         "and used for Người chốt đơn")
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
    ap.add_argument("--retrim", action="store_true",
                    help="trim files already in the inbox, in place, then exit "
                         "(fixes captures taken before trimming existed)")
    ap.add_argument("--no-trim", action="store_true",
                    help="keep everything after the deposit line instead of cutting the "
                         "order there (default is to trim trailing chatter)")
    ap.add_argument("--no-split", action="store_true",
                    help="save the whole copied text as one file, without splitting on order headers")
    args = ap.parse_args()

    today = date.today()
    month = args.month or today.month
    year = args.year or today.year

    cfg_early = Config.load(args.config)
    if args.retrim:
        return retrim_existing(cfg_early.zalo.inbox_dir)

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
          + ("  [NO-SPLIT]" if args.no_split else "")
          + ("  [NO-TRIM]" if args.no_trim else "") + "\n")
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
                result = handle_orders(current, cfg, month, year,
                                       all_months=args.all_months,
                                       trim=not args.no_trim,
                                       closer=args.closer)
                saved = result.saved
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
