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
        return _plain_amount(m.group())

    return None


# An order total is never a few thousand đồng, and never half a billion. These bracket
# the two readings of a bare number so the implausible one can be discarded.
PLAUSIBLE_MIN = 100_000
PLAUSIBLE_MAX = 500_000_000


def _as_millions(raw: str) -> float | None:
    """Read a bare number as a count of millions: "5.800" -> 5.8, "11.500" -> 11.5."""
    cleaned = raw.replace(",", ".").replace(" ", "").strip(".")
    parts = [p for p in cleaned.split(".") if p]
    if not parts or not all(p.isdigit() for p in parts):
        return None
    if len(parts) == 1:
        return float(parts[0])
    return float(parts[0] + "." + "".join(parts[1:]))


def _plain_amount(raw: str) -> int | None:
    """Interpret a number written without a tr/k suffix.

    The shop writes totals in millions with a decimal separator -- "Tổng 5.800" is
    5,800,000, not 5,800 -- while a full amount is sometimes written out as
    "6.000.000". Both are digits and separators, so the two readings are told apart
    by which one lands in a plausible range for an order rather than by the notation.
    """
    digits = re.sub(r"[.,\s]", "", raw)
    if not digits.isdigit():
        return None
    literal = int(digits)

    # Already a believable amount in đồng: "6.000.000", "17500000".
    if literal >= PLAUSIBLE_MIN:
        return literal

    millions = _as_millions(raw)
    if millions is None:
        return literal
    scaled = int(round(millions * MILLION))
    # If treating it as millions produces something absurd, the literal was right.
    return scaled if scaled <= PLAUSIBLE_MAX else literal


def format_vnd(value: int | None) -> str:
    return "" if value is None else f"{value:,}".replace(",", ".")
