"""Splitting a pasted chat into orders, by model instead of by regular expression.

The regexes in scripts/zalo_capture.py are not wrong; they are brittle in one specific
way. Each encodes a phrasing someone had already seen, so every phrasing nobody
anticipated was lost in silence -- a header written "(Tên KH)" rather than "- Tên KH"
dropped whole orders, "gương cộc" truncated an order at its first line, and a revision
worded outside a nine-phrase list vanished with no counter and no log. Silence is the
problem: each of those was found by reconciling a spreadsheet weeks later.

A model reads intent, so an unfamiliar phrasing becomes a judgement call rather than a
miss. What it must not do is convert money or invent structure -- config/schema.senkahomes.yaml
already forbids converting money in the field extraction, for the reason that governs this
whole pipeline: the shop reconciles totals against its own workbook by hand, and a figure
that can differ between two runs of the same input cannot be reconciled at all.

So this module answers only "which orders are in this text, and what later message belongs
to which order". Conversion, the order key, the month filter and the workbook stay exactly
where they are.

Runs in shadow mode first (`extract.ai_segmentation: shadow`): both segmenters run, only
the regex result is used, and disagreements are reported. That turns "how often is the
model wrong on our own data" from a guess into a number, before anything depends on it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

# What the model must return. Deliberately narrower than the field-extraction schema: this
# call decides boundaries and attribution, not contents, and asking it for the address and
# the item list too would put two different jobs on one answer.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "orders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "header": {"type": "string",
                               "description": "The header line, verbatim, exactly as typed."},
                    "day": {"type": "integer"},
                    "month": {"type": "integer"},
                    "order_number": {"type": "integer"},
                    "customer": {"type": "string",
                                 "description": "Name from the header, or null if it states none."},
                    "date_swapped": {"type": "boolean",
                                     "description": "True if day and month were typed the wrong way round."},
                    "repeats_header": {"type": "boolean",
                                       "description": "True if this header already appeared in this paste."},
                    "body": {"type": "string",
                             "description": "The order's own lines, verbatim, header included."},
                    "updates": {
                        "type": "array",
                        "description": "Later headerless messages changing THIS order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "confidence": {"type": "string", "enum": ["high", "low"]},
                            },
                            "required": ["text", "confidence"],
                        },
                    },
                },
                "required": ["header", "day", "month", "order_number", "body"],
            },
        },
        "leading_fragment": {
            "type": "string",
            "description": "Lines before the first header, when the paste starts mid-order.",
        },
    },
    "required": ["orders"],
}


@dataclass
class Update:
    text: str
    confidence: str = "high"


@dataclass
class SegmentedOrder:
    header: str
    day: int
    month: int
    order_number: int
    body: str
    customer: str | None = None
    date_swapped: bool = False
    repeats_header: bool = False
    updates: list[Update] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, int, int]:
        """Same business identity the regex path uses, so the two are comparable."""
        return (self.day, self.month, self.order_number)


@dataclass
class SegmentResult:
    orders: list[SegmentedOrder] = field(default_factory=list)
    leading_fragment: str | None = None
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None


class JsonCompleter(Protocol):
    """The one provider capability this needs: a system+user prompt in, JSON out."""

    def complete_json(self, system: str, user: str,
                      schema: dict[str, Any]) -> tuple[dict[str, Any], int, int]: ...


def parse_response(data: dict[str, Any]) -> SegmentResult:
    """Model JSON -> SegmentResult, tolerating every shape it might get wrong.

    Kept separate from the call so the parsing is testable without a key, and so a
    malformed field costs one order rather than the whole paste: a segmentation that
    drops silently is the exact failure this work exists to remove.
    """
    result = SegmentResult(leading_fragment=(data.get("leading_fragment") or "").strip() or None)

    for raw in data.get("orders") or []:
        if not isinstance(raw, dict):
            continue
        try:
            day = int(raw["day"])
            month = int(raw["month"])
            number = int(raw["order_number"])
        except (KeyError, TypeError, ValueError):
            log.warning("segmenter returned an order without a usable key: %r", raw)
            continue
        body = str(raw.get("body") or "").strip()
        header = str(raw.get("header") or "").strip()
        if not body and not header:
            continue

        updates: list[Update] = []
        for item in raw.get("updates") or []:
            if isinstance(item, dict) and str(item.get("text") or "").strip():
                confidence = str(item.get("confidence") or "high")
                updates.append(Update(str(item["text"]).strip(),
                                      confidence if confidence in ("high", "low") else "high"))
            elif isinstance(item, str) and item.strip():
                updates.append(Update(item.strip()))

        result.orders.append(SegmentedOrder(
            header=header or body.splitlines()[0],
            day=day, month=month, order_number=number,
            body=body or header,
            customer=(str(raw.get("customer") or "").strip() or None),
            date_swapped=bool(raw.get("date_swapped")),
            repeats_header=bool(raw.get("repeats_header")),
            updates=updates,
        ))
    return result


def segment(completer: JsonCompleter, text: str, month: int, year: int) -> SegmentResult:
    """One segmentation call. Never raises: a failure here must fall back, not abort.

    The paste is already on disk by the time this runs (see rawpaste), so returning an
    error costs a retry over stored text rather than a second trip through Zalo.
    """
    from .extract.prompt import build_segment_prompt

    system, user = build_segment_prompt(text, month, year)
    try:
        data, input_tokens, output_tokens = completer.complete_json(system, user, RESPONSE_SCHEMA)
    except Exception as exc:
        log.error("segmentation failed: %s", exc)
        return SegmentResult(error=f"{type(exc).__name__}: {exc}")

    if isinstance(data, str):                      # provider handed back raw text
        try:
            data = json.loads(data)
        except ValueError as exc:
            return SegmentResult(error=f"unparseable response: {exc}")
    if not isinstance(data, dict):
        return SegmentResult(error=f"unexpected response type {type(data).__name__}")

    result = parse_response(data)
    result.input_tokens, result.output_tokens = input_tokens, output_tokens
    return result


# ------------------------------------------------------------------ shadow comparison

@dataclass
class Disagreement:
    """One difference between the two segmenters, in terms a human can act on."""
    kind: str                  # only_ai | only_regex | extra_updates | date_swap
    key: tuple[int, int, int] | None
    detail: str

    def __str__(self) -> str:
        where = f"{self.key[0]}/{self.key[1]} đơn {self.key[2]}" if self.key else "-"
        return f"[{self.kind}] {where}: {self.detail}"


def compare(ai: SegmentResult, regex_blocks: list[Any]) -> list[Disagreement]:
    """What the model saw that the regexes did not, and the reverse.

    Pure, and takes the regex blocks as plain objects with .key/.customer, so the whole
    comparison is testable without a provider, a key, or a network.
    """
    if not ai.ok:
        return [Disagreement("error", None, ai.error or "unknown")]

    ai_by_key = {o.key: o for o in ai.orders}
    regex_by_key = {b.key: b for b in regex_blocks}
    out: list[Disagreement] = []

    for key in ai_by_key.keys() - regex_by_key.keys():
        order = ai_by_key[key]
        out.append(Disagreement("only_ai", key,
                                f"model found an order the splitter missed: {order.header!r}"))
    for key in regex_by_key.keys() - ai_by_key.keys():
        block = regex_by_key[key]
        out.append(Disagreement("only_regex", key,
                                f"splitter found an order the model missed: {block.header!r}"))

    for key, order in ai_by_key.items():
        if key not in regex_by_key:
            continue
        if order.updates:
            confident = sum(1 for u in order.updates if u.confidence == "high")
            out.append(Disagreement(
                "extra_updates", key,
                f"{len(order.updates)} revision(s) attached ({confident} high confidence): "
                + " | ".join(u.text.replace("\n", " ⏎ ")[:70] for u in order.updates)))
        if order.date_swapped != bool(getattr(regex_by_key[key], "date_swapped", False)):
            out.append(Disagreement("date_swap", key,
                                    f"model says swapped={order.date_swapped}, "
                                    f"splitter says {bool(regex_by_key[key].date_swapped)}"))
    return out


def summarise(ai: SegmentResult, regex_blocks: list[Any],
              disagreements: list[Disagreement]) -> str:
    """One line for the log. Counts first, so a quiet run is quiet."""
    if not ai.ok:
        return f"shadow segmentation failed: {ai.error}"
    counts: dict[str, int] = {}
    for d in disagreements:
        counts[d.kind] = counts.get(d.kind, 0) + 1
    detail = ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items())) or "no disagreement"
    return (f"shadow: model {len(ai.orders)} order(s), splitter {len(regex_blocks)} — {detail} "
            f"({ai.input_tokens}+{ai.output_tokens} tokens)")


MODES = ("off", "shadow", "on")


def completer_for(cfg) -> JsonCompleter | None:
    """Build the configured provider as a JSON completer, or None if unusable.

    Returns None rather than raising on a missing key or an uninstalled SDK: segmentation
    is an enhancement to capture, and capture must not start failing because a key
    expired. The caller falls back to the regex splitter and says so.
    """
    from .extract.base import extractor_class

    try:
        cls = extractor_class(cfg.extract.provider)
        if cls.key_problem() is not None:
            log.warning("ai_segmentation is on but %s has no usable key — using the "
                        "regex splitter", cfg.extract.provider)
            return None
        return cls(cfg.extract, cfg.load_schema())
    except Exception as exc:
        log.warning("ai_segmentation is on but the provider could not be built (%s) — "
                    "using the regex splitter", exc)
        return None


def run_shadow(cfg, text: str, blocks: list[Any], month: int, year: int) -> list[str]:
    """Segment with the model, compare against the regexes, and report. Changes nothing.

    Returns lines for the caller to log or print. Never raises, and never touches what
    was captured -- that is the whole point of shadow mode.
    """
    completer = completer_for(cfg)
    if completer is None:
        return []
    result = segment(completer, text, month, year)
    disagreements = compare(result, blocks)
    lines = [summarise(result, blocks, disagreements)]
    lines.extend(f"  {d}" for d in disagreements)
    return lines


SHADOW_LOG = "shadow.log"


def log_shadow(inbox, lines: list[str]) -> None:
    """Append one shadow run to a file beside the stored pastes.

    A file rather than stdout because the browser front end redirects stdout into a sink,
    and shadow mode is meant to be left running over a week of ordinary capture and read
    afterwards. Findings nobody can find are the same as no findings.
    """
    from datetime import datetime

    from .rawpaste import store_dir

    if not lines:
        return
    try:
        directory = store_dir(inbox)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / SHADOW_LOG).open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} ===\n")
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        log.warning("could not write the shadow log (%s)", exc)
