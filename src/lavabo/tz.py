"""Timezone lookup that does not take an export down with it.

`zoneinfo` reads the IANA database from the operating system, and **Windows does not ship
one**. On a Windows machine without the `tzdata` package, `ZoneInfo("Asia/Ho_Chi_Minh")`
raises `ZoneInfoNotFoundError` — which used to surface at the end of a capture session, as
a traceback in place of the workbook.

`tzdata` is a dependency now, so a fresh install has the database. This module covers the
installs that already exist: a missing zone degrades to UTC with one actionable warning
instead of losing work that took an afternoon to capture. Times shift by the offset (7
hours for Vietnam), which is wrong but visible and fixable, where a crash is neither.
"""

from __future__ import annotations

import logging
from datetime import timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

# Warned-about names, so a per-conversation call site does not print this 90 times.
_warned: set[str] = set()


def zone(name: str) -> tzinfo:
    """The named zone, or UTC if this machine has no timezone database."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        if name not in _warned:
            _warned.add(name)
            log.warning(
                "no timezone database for %r (%s) — falling back to UTC, so times will be "
                "off by that zone's offset. Fix with: pip install tzdata", name, exc,
            )
        return timezone.utc


def problem(name: str) -> str | None:
    """Describe why a zone cannot be loaded, or None if it can. For preflight checks."""
    try:
        ZoneInfo(name)
        return None
    except (ZoneInfoNotFoundError, ValueError) as exc:
        return (f"timezone {name!r} is not available on this machine ({exc}). "
                "Run: pip install tzdata")
