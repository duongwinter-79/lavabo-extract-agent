#!/usr/bin/env python3
"""Capture Zalo conversations straight from the clipboard into data/inbox/zalo/.

Zalo's export is encrypted and there is no per-conversation export, so the transcript
has to come out through the UI. This removes everything around that except the copy
itself -- no Notepad, no Save As dialog, no thinking about filenames.

    Your loop, per conversation:  click it -> scroll to top -> Ctrl+A -> Ctrl+C
    The script does:              detect, name, deduplicate, write the .txt

The customer name is derived from the transcript itself: the script parses sender
names with the same patterns the ingest connector uses, discards the names listed in
`zalo.own_names`, and takes the most frequent remaining name. That is the "displayName"
you asked about, recovered from content rather than from the app's internals.

Nothing is sent anywhere -- this only reads the clipboard and writes local files.

Usage:
    python scripts/zalo_capture.py                 # watch until Ctrl+C
    python scripts/zalo_capture.py --once          # capture a single conversation
    python scripts/zalo_capture.py --name "Tran B" # force the name for the next capture
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lavabo.config import Config  # noqa: E402
from lavabo.connectors.zalo_export import DEFAULT_PATTERNS  # noqa: E402

POLL_SECONDS = 0.5
MIN_TRANSCRIPT_CHARS = 40
MIN_MATCHED_LINES = 2
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


def derive_name(text: str, own: set[str]) -> tuple[str | None, Counter[str]]:
    """Most frequent sender that is not us. None if undecidable."""
    senders = parse_senders(text)
    others = Counter({n: c for n, c in senders.items() if n.casefold() not in own})
    if not others:
        return None, senders
    return others.most_common(1)[0][0], senders


def sanitize(name: str) -> str:
    name = re.sub(INVALID_FILENAME, "", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    return name[:MAX_NAME_LEN] or "unnamed"


def looks_like_transcript(text: str) -> bool:
    if len(text) < MIN_TRANSCRIPT_CHARS:
        return False
    return sum(parse_senders(text).values()) >= MIN_MATCHED_LINES


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
    detail = f"{count} messages" if count else "format not yet recognised"
    print(f"  saved {path.name}  ({detail}, {len(text):,} chars)")
    return path


def explain_rejection(text: str) -> str:
    """Why looks_like_transcript() said no — shown in --debug."""
    if len(text) < MIN_TRANSCRIPT_CHARS:
        return f"only {len(text)} chars (need {MIN_TRANSCRIPT_CHARS}+)"
    matched = sum(parse_senders(text).values())
    return (f"{len(text):,} chars but only {matched} line(s) matched a sender pattern "
            f"(need {MIN_MATCHED_LINES}+)")


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
    args = ap.parse_args()

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
    print(f"Watching clipboard -> {cfg.zalo.inbox_dir}")
    print(f"({len(seen)} conversation(s) already captured)"
          + ("  [RAW MODE: saving anything copied]" if args.raw else "")
          + ("  [DEBUG]" if args.debug else "") + "\n")
    print("In Zalo: open a conversation, scroll to the TOP of what you want, then")
    print("         select all and copy (Cmd+A/Cmd+C on macOS, Ctrl+A/Ctrl+C on Windows).")
    print("         Repeat per conversation. Ctrl+C here to stop — always Ctrl.\n")
    if not args.raw:
        print("Nothing happening when you copy? Re-run with --debug to see why, or")
        print("--raw to save the text anyway and let the parser be tuned afterwards.\n")

    last = read_clipboard() or ""
    captured = 0

    try:
        while True:
            time.sleep(POLL_SECONDS)
            current = read_clipboard() or ""

            if current == last or not current:
                continue
            last = current

            recognised = looks_like_transcript(current)

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

            if handle(current, cfg, own, args.name):
                seen.add(digest)
                captured += 1
                args.name = None       # a forced name applies once only
                if args.once:
                    break

    except KeyboardInterrupt:
        print()

    print(f"\n{captured} conversation(s) captured. Next: lavabo ingest --source zalo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
