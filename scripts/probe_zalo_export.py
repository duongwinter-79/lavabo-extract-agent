#!/usr/bin/env python3
"""Inspect whatever Zalo PC's "Export data / Sao luu du lieu" produced.

Answers one question: is the output machine-readable, or is it an opaque
restore-only blob?

Deliberately does NOT print message content -- only structure, sizes and format
verdicts -- so the report is safe to paste back into a chat or an issue.

Usage:
    python scripts/probe_zalo_export.py <path-to-export.zip-or-folder> [-o report.md]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
from collections import Counter
from pathlib import Path

TEXT_HINT_BYTES = 4096
SAMPLE_ENTRIES = 40

# Magic numbers worth recognising inside a backup archive.
SIGNATURES: list[tuple[bytes, str]] = [
    (b"SQLite format 3\x00", "SQLite database"),
    (b"PK\x03\x04", "zip archive (nested)"),
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"%PDF-", "PDF"),
    (b"\x1f\x8b", "gzip stream"),
    (b"ustar", "tar archive"),
]


def sniff(blob: bytes) -> str:
    """Classify a byte sample into a coarse format verdict."""
    if not blob:
        return "empty"

    for magic, label in SIGNATURES:
        if blob.startswith(magic) or (magic == b"ustar" and magic in blob[:512]):
            return label

    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return "binary / encrypted (not valid UTF-8)"

    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return "JSON (or JSON-like text)"
    if stripped.lower().startswith(("<!doctype", "<html", "<?xml")):
        return "HTML/XML"

    printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in text)
    if printable / len(text) > 0.9:
        return "plain text"
    return "binary / encrypted (low printable ratio)"


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def probe_zip(path: Path, lines: list[str]) -> None:
    lines.append("## Archive")

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        head = path.open("rb").read(16)
        lines.append(
            "- **Not a readable zip.** Standard zip tooling cannot open it.\n"
            f"- First bytes: `{head.hex(' ')}`\n"
            "- **Verdict: encrypted or proprietary container.** This matches the documented\n"
            "  behaviour of Zalo's backup: restore-only, not an export.\n"
            "  -> Zalo must go down the manual-transcript path."
        )
        return

    with zf:
        entries = [i for i in zf.infolist() if not i.is_dir()]
        encrypted = [i for i in entries if i.flag_bits & 0x1]

        lines.append(f"- Opens as a zip: **yes** ({len(entries)} files)")
        lines.append(f"- Total uncompressed: {human(sum(i.file_size for i in entries))}")

        if encrypted:
            lines.append(
                f"- **{len(encrypted)} of {len(entries)} entries are password-protected.** "
                "Contents unreadable without the backup password you set in Zalo."
            )

        exts = Counter((Path(i.filename).suffix.lower() or "(none)") for i in entries)
        lines.append("\n### Extensions")
        for ext, n in exts.most_common(20):
            lines.append(f"- `{ext}` x{n}")

        roots: Counter[str] = Counter()
        for info in entries:
            parts = Path(info.filename).parts
            # A single part means a file at the archive root, not a directory.
            roots[f"{parts[0]}/" if len(parts) > 1 else parts[0]] += 1

        lines.append("\n### Top-level entries")
        for root, n in roots.most_common(20):
            lines.append(f"- `{root}` x{n}")

        lines.append("\n### Format check (largest entries)")
        readable = 0
        biggest = sorted(entries, key=lambda i: i.file_size, reverse=True)[:SAMPLE_ENTRIES]

        for info in biggest:
            if info.flag_bits & 0x1:
                verdict = "password-protected (skipped)"
            else:
                try:
                    with zf.open(info) as fh:
                        verdict = sniff(fh.read(TEXT_HINT_BYTES))
                except (RuntimeError, zipfile.BadZipFile) as exc:
                    verdict = f"unreadable ({type(exc).__name__})"

            if any(k in verdict for k in ("JSON", "plain text", "SQLite", "HTML")):
                readable += 1
            lines.append(f"- `{info.filename}` ({human(info.file_size)}) -> {verdict}")

        lines.append("\n## Verdict")
        if readable:
            lines.append(
                f"**{readable} readable structured entries found.** This is better than\n"
                "expected -- Zalo may be automatable after all. Send this report back and\n"
                "the Zalo connector gets rewritten to parse the archive directly."
            )
        else:
            lines.append(
                "**No readable message data.** Media and opaque blobs only, as expected.\n"
                "Confirms the finding in `docs/01-source-verification.md`: Zalo's export is a\n"
                "restore-only backup. Proceed with the manual transcript path in\n"
                "`docs/03-zalo-runbook.md`."
            )


def probe_dir(path: Path, lines: list[str]) -> None:
    files = [p for p in path.rglob("*") if p.is_file()]
    lines.append(f"## Folder\n- {len(files)} files, {human(sum(p.stat().st_size for p in files))}")

    exts = Counter(p.suffix.lower() or "(none)" for p in files)
    lines.append("\n### Extensions")
    for ext, n in exts.most_common(20):
        lines.append(f"- `{ext}` x{n}")

    lines.append("\n### Format check (largest files)")
    readable = 0
    for p in sorted(files, key=lambda p: p.stat().st_size, reverse=True)[:SAMPLE_ENTRIES]:
        verdict = sniff(p.open("rb").read(TEXT_HINT_BYTES))
        if any(k in verdict for k in ("JSON", "plain text", "SQLite", "HTML")):
            readable += 1
        lines.append(f"- `{p.relative_to(path)}` ({human(p.stat().st_size)}) -> {verdict}")

    lines.append(
        f"\n## Verdict\n**{readable} readable structured files.**"
        if readable
        else "\n## Verdict\n**No readable message data found.**"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path, help="Zalo export .zip or extracted folder")
    ap.add_argument("-o", "--out", type=Path, help="write the report to a file as well")
    args = ap.parse_args()

    if not args.path.exists():
        print(f"error: {args.path} does not exist", file=sys.stderr)
        return 2

    lines = [
        "# Zalo export probe",
        "",
        f"- Path: `{args.path}`",
        f"- Size: {human(args.path.stat().st_size) if args.path.is_file() else 'folder'}",
        "",
    ]

    if args.path.is_dir():
        probe_dir(args.path, lines)
    elif zipfile.is_zipfile(args.path) or args.path.suffix.lower() in {".zip", ".crypt"}:
        probe_zip(args.path, lines)
    else:
        verdict = sniff(args.path.open("rb").read(TEXT_HINT_BYTES))
        lines.append(f"## Single file\n- Format: **{verdict}**")

    report = "\n".join(lines)
    print(report)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
        print(f"\n[written to {args.out}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
