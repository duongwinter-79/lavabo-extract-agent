"""Vietnamese money shorthand -> VND integer.

Deliberately not the model's job. "2tr5" means 2,500,000 and "29tr500" means 29,500,000;
that positional rule is easy to state and easy for a language model to get subtly wrong,
and a money column is the worst place to accept "probably right". The model returns the
text it found, verbatim, and this converts it.

    29tr        -> 29_000_000
    2tr5        -> 2_500_000
    12tr300     -> 12_300_000
    500k        -> 500_000
    6.000.000   -> 6_000_000
"""

from __future__ import annotations

import re

MILLION = 1_000_000
THOUSAND = 1_000

# "12tr300", "2tr5", "29tr", "1 triệu 5"
_MILLIONS = re.compile(
    r"(?P<whole>\d+)\s*(?:tr|triệu|trieu|củ|cu)\s*(?P<frac>\d+)?",
    re.IGNORECASE,
)
# "500k", "800 nghìn"
_THOUSANDS = re.compile(r"(?P<whole>\d+)\s*(?:k|nghìn|nghin|ngàn|ngan)\b", re.IGNORECASE)
# Bare "6.000.000" / "6,000,000" / "17500000"
_PLAIN = re.compile(r"\d[\d.,\s]*")


def parse_vnd(text: str | None) -> int | None:
    """Best-effort VND amount from free text. None when nothing numeric is present."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)

    s = str(text).strip().lower()
    if not s:
        return None

    if m := _MILLIONS.search(s):
        total = int(m["whole"]) * MILLION
        if frac := m["frac"]:
            # Digits after "tr" are a decimal fraction of a million, by position:
            # 5 -> .5, 50 -> .50, 500 -> .500. All three mean 500,000.
            total += int(round(int(frac) / (10 ** len(frac)) * MILLION))
        return total

    if m := _THOUSANDS.search(s):
        return int(m["whole"]) * THOUSAND

    if m := _PLAIN.search(s):
        digits = re.sub(r"[.,\s]", "", m.group())
        if digits.isdigit():
            return int(digits)

    return None


def format_vnd(value: int | None) -> str:
    return "" if value is None else f"{value:,}".replace(",", ".")
