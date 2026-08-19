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

import hashlib
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
    # Answered from the cache, so nothing was sent and nothing was billed. Reported,
    # because a token count that looks the same whether or not money changed hands is a
    # token count nobody can use to decide anything.
    cached: bool = False
    # The model's answer before this module made sense of it. Kept so the cache can store
    # what was SAID rather than what was understood: a parser fix then reaches every
    # cached answer for free, while a prompt change correctly misses the cache instead.
    raw: Any = None

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


# Measured, not guessed: a real month of this chat -- ~900 lines, 69 orders -- answered in
# 4,952 output tokens, so ~5.5 per input line. Doubled, because the cost of overshooting
# is a few unused tokens while the cost of undershooting is a truncated answer, and a
# truncated answer is what returned 1 order out of 69.
TOKENS_PER_LINE = 12
# A frame holds a couple of orders and the answer quotes them in full, so this is
# sized per FRAME and generously: running out mid-answer is the failure that
# returned 1 order out of 69 on the text path.
TOKENS_PER_VIDEO_FRAME = 900
MIN_OUTPUT_TOKENS = 4096
MAX_OUTPUT_TOKENS = 64_000


def output_budget(lines: int) -> int:
    """How much room to give the answer, from the size of the question.

    The configured max_tokens is shared with field extraction, where 4096 is ample for one
    order's fields -- and silently is not, here, for a whole month of them at once. Sizing
    from the input is the only thing that keeps working as the chat grows.
    """
    return max(MIN_OUTPUT_TOKENS, min(MAX_OUTPUT_TOKENS, lines * TOKENS_PER_LINE))


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
    result.raw = data
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


def compare(ai: SegmentResult, regex_blocks: list[Any],
            ai_blocks: list[Any] | None = None) -> list[Disagreement]:
    """What the model saw that the regexes did not, and the reverse.

    `ai_blocks` is the model's answer AFTER conversion -- which is to say after the
    deterministic day/month correction still runs over it. Compare without that and the
    log reports differences that do not exist in what gets saved: the first good live run
    showed "8/3 đơn 1" as an order the model missed and "3/8 đơn 1" as one it invented,
    when they are the same order and the converted output already had it right. Reporting
    a phantom `only_regex` is not a harmless inaccuracy -- that line is the one that is
    supposed to stop the switch.

    Pure, and takes both sides as plain objects with .key/.header, so the whole comparison
    is testable without a provider, a key, or a network.
    """
    if not ai.ok:
        return [Disagreement("error", None, ai.error or "unknown")]

    ai_by_key = {b.key: b for b in (ai_blocks if ai_blocks is not None else ai.orders)}
    regex_by_key = {b.key: b for b in regex_blocks}
    out: list[Disagreement] = []

    for key in ai_by_key.keys() - regex_by_key.keys():
        block = ai_by_key[key]
        out.append(Disagreement("only_ai", key,
                                f"model found an order the splitter missed: {block.header!r}"))
    for key in regex_by_key.keys() - ai_by_key.keys():
        block = regex_by_key[key]
        out.append(Disagreement("only_regex", key,
                                f"splitter found an order the model missed: {block.header!r}"))

    for key, block in ai_by_key.items():
        if key not in regex_by_key:
            continue
        updates = _updates_of(block)
        if updates:
            confident = sum(1 for text, confidence in updates if confidence == "high")
            out.append(Disagreement(
                "extra_updates", key,
                f"{len(updates)} revision(s) attached ({confident} high confidence): "
                + " | ".join(text.replace("\n", " ⏎ ")[:70] for text, _ in updates)))
    return out


def _updates_of(block: Any) -> list[tuple[str, str]]:
    """Revisions on either shape: a SegmentedOrder, or the OrderBlock it converts into."""
    if hasattr(block, "ai_updates"):
        return list(block.ai_updates)
    return [(u.text, u.confidence) for u in getattr(block, "updates", [])]


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
    cost = ("đã có sẵn, không tốn token" if ai.cached
            else f"{ai.input_tokens}+{ai.output_tokens} tokens"
                 + (f", stopped: {ai.finish_reason}" if ai.finish_reason else ""))
    line = (f"shadow: model {len(ai.orders)} order(s), splitter {len(regex_blocks)} — "
            f"{detail} ({cost})")

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
    """Segment one paste with the configured provider, or from the cache.

    None means the provider could not be used at all -- no key, no SDK, misconfigured --
    which is different from a call that was made and failed, and the caller reports them
    differently. Never raises: capture must survive anything that happens out here.

    The cache is checked BEFORE the provider is even built, so a repeat paste costs
    nothing at all -- not a call, not a key check, not the import of an SDK.
    """
    from .extract.prompt import number_lines

    inbox = cfg.zalo.inbox_dir
    key = cache_key(text.encode("utf-8"), month, year, cfg.extract.model)
    if (cached := load_cache(inbox).get(key)) is not None:
        result = parse_response(cached, number_lines(text)[1])
        result.cached = True
        return result

    completer = completer_for(cfg)
    if completer is None:
        return None
    result = segment(completer, text, month, year)
    # Only a complete answer is worth keeping. A failure, or one cut off at max_tokens,
    # would otherwise be served back forever -- turning a bad minute at the provider into
    # a permanent wrong answer for that paste.
    if result.ok and not result.truncated and result.raw is not None:
        remember(inbox, key, result.raw)
    return result


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


# ------------------------------------------------------------------ from video

# The video path cannot use line numbers -- there are none in a photograph -- so the model
# transcribes, and every order read this way is flagged for a human to check against the
# total. `partial` and `frame` are what let this program clean up after the medium: an
# order clipped by a frame edge is replaced by the fuller copy from a neighbouring frame,
# by the same merge_into that has always preferred a fuller capture.
VIDEO_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "orders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "frame": {"type": "integer",
                              "description": "Which frame this order was read from, 1-based."},
                    "header": {"type": "string",
                               "description": "The header line, transcribed exactly."},
                    "day": {"type": "integer"},
                    "month": {"type": "integer"},
                    "order_number": {"type": "integer"},
                    "customer": {"type": "string",
                                 "description": "Name from the header, or null if it states none."},
                    "date_swapped": {"type": "boolean"},
                    "repeats_header": {"type": "boolean"},
                    "partial": {"type": "boolean",
                                "description": "True if a frame edge cut this order off."},
                    "body": {"type": "string",
                             "description": "The whole order, transcribed exactly, header included."},
                    "updates": {
                        "type": "array",
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
                "required": ["frame", "header", "day", "month", "order_number", "body"],
            },
        },
    },
    "required": ["orders"],
}


def parse_video_response(data: dict[str, Any]) -> SegmentResult:
    """Model JSON -> SegmentResult, for text the model transcribed rather than pointed at.

    Partial reads are dropped when a whole read of the same order exists, and kept when it
    does not -- half an order is worth reviewing, but not in preference to the whole one
    sitting in the next frame.
    """
    result = SegmentResult()
    best: dict[tuple[int, int, int], tuple[bool, SegmentedOrder]] = {}

    for raw in data.get("orders") or []:
        if not isinstance(raw, dict):
            continue
        try:
            day, month = int(raw["day"]), int(raw["month"])
            number = int(raw["order_number"])
        except (KeyError, TypeError, ValueError):
            log.warning("video segmenter returned an order without a usable key: %r", raw)
            result.rejected += 1
            continue
        body = str(raw.get("body") or "").strip()
        header = str(raw.get("header") or "").strip()
        if not body and not header:
            result.rejected += 1
            continue

        updates: list[Update] = []
        for item in raw.get("updates") or []:
            if isinstance(item, dict) and str(item.get("text") or "").strip():
                confidence = str(item.get("confidence") or "high")
                updates.append(Update(str(item["text"]).strip(),
                                      confidence if confidence in ("high", "low") else "high"))

        order = SegmentedOrder(
            header=header or body.splitlines()[0],
            day=day, month=month, order_number=number,
            body=body or header,
            customer=(str(raw.get("customer") or "").strip() or None),
            date_swapped=bool(raw.get("date_swapped")),
            repeats_header=bool(raw.get("repeats_header")),
            updates=updates,
        )
        partial = bool(raw.get("partial"))
        key = order.key
        if key not in best:
            best[key] = (partial, order)
            continue
        # Same order, filmed again. Prefer a whole read over a clipped one, then the
        # longer text -- the same rule merge_into applies, applied early so the frames
        # never reach disk as competing versions of one order.
        was_partial, kept = best[key]
        better = (was_partial and not partial) or (
            was_partial == partial and len(order.body) > len(kept.body))
        if better:
            best[key] = (partial, order)

    result.orders = [order for _, order in best.values()]
    return result


def segment_video(completer: JsonCompleter, images: list[bytes], month: int, year: int,
                  *, mime_type: str = "image/png") -> SegmentResult:
    """One segmentation call over screen-recording frames. Never raises."""
    from .extract.prompt import build_video_prompt

    if not images:
        return SegmentResult(error="no frames to read")

    system, user = build_video_prompt(len(images), month, year)
    budget = max(MIN_OUTPUT_TOKENS,
                 min(MAX_OUTPUT_TOKENS, len(images) * TOKENS_PER_VIDEO_FRAME))
    try:
        answer = completer.complete_json_images(system, user, images,
                                                VIDEO_RESPONSE_SCHEMA,
                                                max_tokens=budget, mime_type=mime_type)
    except Exception as exc:
        log.error("video segmentation failed: %s", exc)
        return SegmentResult(error=f"{type(exc).__name__}: {exc}")

    data = answer.data
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError as exc:
            return SegmentResult(error=f"unparseable response: {exc}",
                                 finish_reason=answer.finish_reason)
    if not isinstance(data, dict):
        return SegmentResult(error=f"unexpected response type {type(data).__name__}",
                             finish_reason=answer.finish_reason)

    result = parse_video_response(data)
    result.raw = data
    result.input_tokens = answer.input_tokens
    result.output_tokens = answer.output_tokens
    result.finish_reason = answer.finish_reason
    return result


def run_video(cfg, images: list[bytes], month: int, year: int) -> SegmentResult | None:
    """Read orders off screen-recording frames, or from the cache.

    Cached for the same reason as the text path and more urgently: a month of frames is
    the most expensive call this app makes, and pressing the button twice is an easy
    mistake to make while waiting for the first one.
    """
    inbox = cfg.zalo.inbox_dir
    key = cache_key(b"".join(images), month, year, cfg.extract.model)
    if (cached := load_cache(inbox).get(key)) is not None:
        result = parse_video_response(cached)
        result.cached = True
        return result

    completer = completer_for(cfg)
    if completer is None:
        return None
    result = segment_video(completer, images, month, year)
    if result.ok and not result.truncated and result.raw is not None:
        remember(inbox, key, result.raw)
    return result


# ------------------------------------------------------------------- caching

# Segmentation is the only AI call this app makes that had no cache, and it sits on the
# path people are told to use freely: capture a month in OVERLAPPING chunks, re-paste
# whenever you are unsure. Every one of those pastes was a fresh call over text already
# segmented -- three identical pastes cost three calls, and a month captured in ten
# overlapping sweeps paid for most of its content several times.
#
# Keyed on what could change the answer and nothing else: the text, the prompt version,
# the model, and the month being captured -- the last because the target month is written
# into the prompt and the day/month transposition rule turns on it, so the same paste
# genuinely has different right answers for July and August.
#
# The RAW model response is stored rather than the parsed orders. A parser fix then
# reaches old answers for free, while a prompt change correctly misses the cache.
SEGMENT_CACHE = "segments.json"
CACHE_VERSION = 1
KEEP_CACHED = 500


def _cache_path(inbox):
    from .rawpaste import store_dir
    return store_dir(inbox) / SEGMENT_CACHE


def cache_key(payload: bytes, month: int, year: int, model: str) -> str:
    from .extract.prompt import SEGMENT_PROMPT_VERSION

    digest = hashlib.sha256(payload).hexdigest()[:32]
    return f"{digest}:{SEGMENT_PROMPT_VERSION}:{model}:{month:02d}{year}"


def load_cache(inbox) -> dict[str, Any]:
    path = _cache_path(inbox)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        answers = data.get("answers") if isinstance(data, dict) else None
        return answers if isinstance(answers, dict) else {}
    except (OSError, ValueError, AttributeError) as exc:
        log.warning("could not read %s (%s) — treating as empty", path.name, exc)
        return {}


def save_cache(inbox, answers: dict[str, Any]) -> None:
    # Oldest entries drop first. Python dicts keep insertion order, and every write
    # re-inserts the key it just used, so this is a least-recently-used bound.
    if len(answers) > KEEP_CACHED:
        answers = dict(list(answers.items())[-KEEP_CACHED:])
    path = _cache_path(inbox)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": CACHE_VERSION, "answers": answers},
                                   ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write the segmentation cache (%s)", exc)


def remember(inbox, key: str, payload: Any) -> None:
    answers = load_cache(inbox)
    answers.pop(key, None)                 # re-insert, so it counts as recently used
    answers[key] = payload
    save_cache(inbox, answers)
