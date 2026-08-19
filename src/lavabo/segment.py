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
import re
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Protocol

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
                    "header_line": {"type": "integer",
                                    "description": "Line number of the order's header."},
                    "end_line": {"type": "integer",
                                 "description": "Line number of the order's last line."},
                    "day": {"type": "integer"},
                    "month": {"type": "integer"},
                    "order_number": {"type": "integer"},
                    "customer": {"type": "string",
                                 "description": "Name from the header, or null if it states none."},
                    "date_swapped": {"type": "boolean",
                                     "description": "True if day and month were typed the wrong way round."},
                    "repeats_header": {"type": "boolean",
                                       "description": "True if this header already appeared in this paste."},
                    "updates": {
                        "type": "array",
                        "description": "Later headerless messages changing THIS order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "start_line": {"type": "integer"},
                                "end_line": {"type": "integer"},
                                "confidence": {"type": "string", "enum": ["high", "low"]},
                            },
                            "required": ["start_line", "end_line", "confidence"],
                        },
                    },
                },
                "required": ["header_line", "end_line", "day", "month", "order_number"],
            },
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
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    # Why the model stopped. "MAX_TOKENS" means the answer was cut off mid-thought, which
    # is the difference between "the model found 1 order" and "the model was interrupted
    # after 1 order" -- indistinguishable in the output, and the first live run cost a day
    # to that ambiguity.
    finish_reason: str = ""
    # Orders thrown away here rather than by the model: an unusable key or line range, and
    # a header line that does not carry the order it was said to. Counted because a
    # segmenter that quietly discards a tenth of its own answer must not look like one
    # that found nine tenths of the orders.
    rejected: int = 0
    miscounted: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def truncated(self) -> bool:
        return self.finish_reason.upper().endswith("MAX_TOKENS")


class Completion(NamedTuple):
    """A provider's answer. A named tuple because this has already grown once -- see
    CaptureResult, where growing a plain tuple silently broke callers by arity."""
    data: Any
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = ""


class JsonCompleter(Protocol):
    """The one provider capability this needs: a system+user prompt in, JSON out."""

    def complete_json(self, system: str, user: str,
                      schema: dict[str, Any], *, max_tokens: int = 0) -> Completion: ...


def _slice(lines: list[str], first: Any, last: Any) -> tuple[str, int, int] | None:
    """Text of lines[first..last], 1-based and inclusive, or None if the range is unusable.

    Clamped rather than rejected at the upper end: a model that overshoots the last line by
    one has still identified the order correctly, and dropping it over that would trade a
    whole order for a rounding error.
    """
    try:
        start_index, end_index = int(first), int(last)
    except (TypeError, ValueError):
        return None
    if start_index < 1 or start_index > len(lines):
        return None
    end_index = max(start_index, min(end_index, len(lines)))
    return "\n".join(lines[start_index - 1:end_index]).strip(), start_index, end_index


def parse_response(data: dict[str, Any], lines: list[str]) -> SegmentResult:
    """Model JSON -> SegmentResult, slicing every piece of text out of `lines`.

    The model returns line numbers only, so the words in the result are the words in the
    paste by construction. What has to be checked instead is that it counted correctly:
    an order whose header line does not actually carry the day, month and order number it
    reported is a miscount, and is dropped with a warning rather than silently filing a
    real order's lines under a wrong key.
    """
    result = SegmentResult()

    for raw in data.get("orders") or []:
        if not isinstance(raw, dict):
            continue
        try:
            day = int(raw["day"])
            month = int(raw["month"])
            number = int(raw["order_number"])
        except (KeyError, TypeError, ValueError):
            log.warning("segmenter returned an order without a usable key: %r", raw)
            result.rejected += 1
            continue

        span = _slice(lines, raw.get("header_line"), raw.get("end_line"))
        if span is None:
            log.warning("segmenter gave an unusable line range for %s/%s đơn %s: %r",
                        day, month, number, raw)
            result.rejected += 1
            continue
        body, header_index, _ = span
        header = lines[header_index - 1].strip()

        if not _header_agrees(header, day, month, number):
            log.warning("segmenter miscounted: line %d is %r, which is not %d/%d đơn %d",
                        header_index, header, day, month, number)
            result.miscounted += 1
            continue

        updates: list[Update] = []
        for item in raw.get("updates") or []:
            if not isinstance(item, dict):
                continue
            piece = _slice(lines, item.get("start_line"), item.get("end_line"))
            if piece is None or not piece[0]:
                continue
            confidence = str(item.get("confidence") or "high")
            updates.append(Update(piece[0], confidence if confidence in ("high", "low") else "high"))

        result.orders.append(SegmentedOrder(
            header=header,
            day=day, month=month, order_number=number,
            body=body,
            customer=(str(raw.get("customer") or "").strip() or None),
            date_swapped=bool(raw.get("date_swapped")),
            repeats_header=bool(raw.get("repeats_header")),
            updates=updates,
        ))
    return result


# Does the line the model pointed at really carry the order it claims? Deliberately loose
# about separators and spacing -- the header shapes vary wildly, and this is checking the
# NUMBERS, not re-parsing the header.
def _header_agrees(header: str, day: int, month: int, number: int) -> bool:
    digits = re.findall(r"\d+", header)
    if len(digits) < 3:
        return False
    values = [int(d) for d in digits[:4]]
    # A swapped date is reported after swapping, so accept either order for the pair.
    return (number in values
            and ((day in values and month in values) or day == month))


# Roughly what one order costs to describe in line numbers, measured from the schema:
# two line numbers, three integers, a name and a couple of flags. Generous on purpose --
# running out of output budget is the failure this exists to prevent.
TOKENS_PER_ORDER = 60
MIN_OUTPUT_TOKENS = 4096
MAX_OUTPUT_TOKENS = 64_000


def output_budget(lines: int) -> int:
    """How much room to give the answer, from the size of the question.

    A month of this shop's chat is ~900 lines and ~70 orders. The configured max_tokens is
    shared with field extraction, where 4096 is ample for one order -- and it silently is
    not, here, for a whole month at once. Sizing it from the input is the only way this
    scales with a chat that keeps growing.
    """
    orders = max(1, lines // 8)                 # ~8 lines per order in this chat
    return max(MIN_OUTPUT_TOKENS, min(MAX_OUTPUT_TOKENS, orders * TOKENS_PER_ORDER))


def segment(completer: JsonCompleter, text: str, month: int, year: int) -> SegmentResult:
    """One segmentation call. Never raises: a failure here must fall back, not abort.

    The paste is already on disk by the time this runs (see rawpaste), so returning an
    error costs a retry over stored text rather than a second trip through Zalo.
    """
    from .extract.prompt import build_segment_prompt

    system, user, lines = build_segment_prompt(text, month, year)
    try:
        answer = completer.complete_json(system, user, RESPONSE_SCHEMA,
                                         max_tokens=output_budget(len(lines)))
    except Exception as exc:
        log.error("segmentation failed: %s", exc)
        return SegmentResult(error=f"{type(exc).__name__}: {exc}")

    data = answer.data
    if isinstance(data, str):                      # provider handed back raw text
        try:
            data = json.loads(data)
        except ValueError as exc:
            return SegmentResult(
                error=f"unparseable response: {exc}"
                      + (" (answer was cut off at max_tokens)" if
                         answer.finish_reason.upper().endswith("MAX_TOKENS") else ""),
                finish_reason=answer.finish_reason)
    if not isinstance(data, dict):
        return SegmentResult(error=f"unexpected response type {type(data).__name__}",
                             finish_reason=answer.finish_reason)

    result = parse_response(data, lines)
    result.input_tokens = answer.input_tokens
    result.output_tokens = answer.output_tokens
    result.finish_reason = answer.finish_reason
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


# Below this share of the splitter's orders, the model has not disagreed about a few
# orders -- it has failed to do the job, and the difference matters because the first is
# worth reading and the second is worth stopping for.
INCOMPLETE_RATIO = 0.9


def summarise(ai: SegmentResult, regex_blocks: list[Any],
              disagreements: list[Disagreement]) -> str:
    """One line for the log. Counts first, so a quiet run is quiet."""
    if not ai.ok:
        return f"shadow segmentation failed: {ai.error}"
    counts: dict[str, int] = {}
    for d in disagreements:
        counts[d.kind] = counts.get(d.kind, 0) + 1
    detail = ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items())) or "no disagreement"
    line = (f"shadow: model {len(ai.orders)} order(s), splitter {len(regex_blocks)} — "
            f"{detail} ({ai.input_tokens}+{ai.output_tokens} tokens"
            + (f", stopped: {ai.finish_reason}" if ai.finish_reason else "") + ")")

    # Said plainly rather than left to be inferred from two counts. The first live run
    # returned 1 order against 69 and the log stated it in a way that read like an
    # ordinary disagreement, which is how it survived a whole capture.
    problems = []
    if ai.truncated:
        problems.append("ANSWER CUT OFF at max_tokens — the model was interrupted, not "
                        "finished. Nothing here is a judgement about those orders.")
    if regex_blocks and len(ai.orders) < len(regex_blocks) * INCOMPLETE_RATIO:
        problems.append(
            f"INCOMPLETE: the model returned {len(ai.orders)} of {len(regex_blocks)} "
            "orders. This is a failed segmentation, not a disagreement — do NOT switch "
            "to 'Dùng AI' on the strength of it.")
    if ai.rejected:
        problems.append(f"{ai.rejected} order(s) unusable (bad key or line range)")
    if ai.miscounted:
        problems.append(f"{ai.miscounted} order(s) pointed at a line that is not their "
                        "header — the model miscounted line numbers")
    return "\n".join([line, *(f"  !! {p}" for p in problems)])


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


def run(cfg, text: str, month: int, year: int) -> SegmentResult | None:
    """Segment one paste with the configured provider.

    None means the provider could not be used at all -- no key, no SDK, misconfigured --
    which is different from a call that was made and failed, and the caller reports them
    differently. Never raises: capture must survive anything that happens out here.
    """
    completer = completer_for(cfg)
    if completer is None:
        return None
    return segment(completer, text, month, year)


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
