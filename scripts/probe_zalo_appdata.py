#!/usr/bin/env python3
"""Look for readable message data in Zalo PC's LOCAL application storage.

This is a different question from `probe_zalo_export.py`. That one asks whether the
*backup archive* is readable (documented answer: no, it is encrypted and restore-only).
This one asks whether the running app leaves anything readable on disk -- Zalo PC is
Electron-based, so it may keep a LevelDB/IndexedDB/SQLite cache of recent messages.

Unverified either way. If it finds a readable store, the Zalo connector can read it
directly and the manual transcript workflow disappears. If it finds nothing, we have
ruled out the last non-manual option short of a Zalo OA.

Prints structure and format verdicts only -- never message content -- so the report is
safe to share.

Usage:
    python scripts/probe_zalo_appdata.py [--path EXTRA_DIR] [-o report.md]
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from probe_zalo_export import human, sniff  # noqa: E402

# Stores worth reporting even though the file itself is binary.
STORE_HINTS = {
    ".ldb": "LevelDB table (Electron IndexedDB)",
    ".leveldb": "LevelDB",
    ".sqlite": "SQLite",
    ".sqlite3": "SQLite",
    ".db": "database (check magic below)",
    ".log": "LevelDB write-ahead log or plain log",
}

INTERESTING_DIRS = {"indexeddb", "local storage", "leveldb", "databases", "session storage"}

# Anything this big is worth a look; tiny files are usually config.
MIN_INTERESTING_BYTES = 8 * 1024
MAX_FILES_LISTED = 30


def candidate_roots(extra: Path | None) -> list[Path]:
    """Common Zalo PC storage locations, per platform."""
    roots: list[Path] = []
    if extra:
        roots.append(extra)

    home = Path.home()
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        roots += [
            appdata / "ZaloPC", appdata / "Zalo", appdata / "ZaloData",
            local / "ZaloPC", local / "Zalo",
            home / "Documents" / "Zalo Received Files",
        ]
    elif sys.platform == "darwin":
        library = home / "Library"
        support = library / "Application Support"
        roots += [support / "Zalo", support / "ZaloPC",
                  home / "Documents" / "Zalo Received Files"]

        # Zalo for Mac ships through the App Store, so it is sandboxed and its real
        # data lives under Containers/<bundle-id>/Data/... rather than in the paths
        # above. Without this the probe reports "not installed" on a Mac that has it.
        for container_base in (library / "Containers", library / "Group Containers"):
            if not container_base.exists():
                continue
            for bundle in container_base.iterdir():
                if not bundle.is_dir() or "zalo" not in bundle.name.casefold():
                    continue
                roots.append(bundle)
                inner = bundle / "Data" / "Library" / "Application Support"
                if inner.exists():
                    roots.append(inner)
    else:
        roots += [home / ".config" / "Zalo", home / ".config" / "ZaloPC",
                  home / ".zalo", home / "Documents" / "Zalo Received Files"]

    # Glob for versioned install dirs like ZaloPC-25.6.1.
    for base in {p.parent for p in roots if p is not None}:
        if base.exists():
            roots += [p for p in base.glob("Zalo*") if p.is_dir()]

    seen, out = set(), []
    for r in roots:
        if r is None:
            continue
        rp = r.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(r)
    return out


def probe_root(root: Path, lines: list[str]) -> int:
    """Report on one storage root. Returns count of readable structured files found."""
    lines.append(f"\n## `{root}`")

    try:
        files = [p for p in root.rglob("*") if p.is_file()]
    except PermissionError as exc:
        lines.append(f"- permission denied: {exc}")
        return 0

    if not files:
        lines.append("- exists but empty")
        return 0

    total = sum(p.stat().st_size for p in files)
    lines.append(f"- {len(files)} files, {human(total)}")

    exts = Counter(p.suffix.lower() or "(none)" for p in files)
    lines.append("- extensions: " + ", ".join(f"`{e}` x{n}" for e, n in exts.most_common(12)))

    hits = [d for d in {p.parent.name.lower() for p in files} if d in INTERESTING_DIRS]
    if hits:
        lines.append(f"- **contains Electron storage dirs: {', '.join(sorted(hits))}**")

    candidates = sorted(
        (p for p in files if p.stat().st_size >= MIN_INTERESTING_BYTES),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )[:MAX_FILES_LISTED]

    if not candidates:
        lines.append("- no files above the size threshold; nothing worth parsing")
        return 0

    lines.append("\n### Largest files")
    readable = 0
    for p in candidates:
        try:
            verdict = sniff(p.open("rb").read(4096))
        except OSError as exc:
            verdict = f"unreadable ({type(exc).__name__})"

        note = STORE_HINTS.get(p.suffix.lower(), "")
        if any(k in verdict for k in ("SQLite", "JSON", "plain text")):
            readable += 1
            verdict = f"**{verdict}**"

        rel = p.relative_to(root)
        lines.append(f"- `{rel}` ({human(p.stat().st_size)}) -> {verdict}"
                     + (f"  _{note}_" if note else ""))

    return readable


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", type=Path, help="additional directory to probe")
    ap.add_argument("-o", "--out", type=Path, help="also write the report to a file")
    args = ap.parse_args()

    lines = ["# Zalo PC local app-data probe", "", f"- Platform: `{sys.platform}`"]

    roots = candidate_roots(args.path)
    found = [r for r in roots if r.exists()]

    lines.append(f"- Checked {len(roots)} candidate location(s), {len(found)} exist")
    if not found:
        lines.append("\n## Verdict\n**No Zalo PC storage found at the usual paths.**\n"
                     "Either Zalo PC is not installed on this machine, or it stores data "
                     "elsewhere. Re-run with `--path <dir>` if you know the install location.")
    else:
        readable = sum(probe_root(r, lines) for r in found)
        lines.append("\n## Verdict")
        if readable:
            lines.append(
                f"**{readable} readable structured file(s) found.** Worth investigating: a "
                "local store would let the Zalo connector read history directly and remove "
                "the manual capture step entirely. Send this report back.\n\n"
                "Note: readable *format* does not guarantee readable *content* -- Electron "
                "LevelDB stores are often obfuscated or keyed opaquely."
            )
        else:
            lines.append(
                "**Nothing readable.** Combined with the encrypted backup, this rules out "
                "automated local extraction. Use `scripts/zalo_capture.py` to make the "
                "manual path fast, or move to a Zalo OA for full automation."
            )

    report = "\n".join(lines)
    print(report)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
        print(f"\n[written to {args.out}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
