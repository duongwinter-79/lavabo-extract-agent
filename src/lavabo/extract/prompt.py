"""Prompt construction. Shared by every provider so results stay comparable.

Bump PROMPT_VERSION on any wording change: it is part of the extraction cache key, so
changing the prompt correctly invalidates previously cached results.
"""

from __future__ import annotations


from ..tz import zone as tz_zone
from ..config import ExtractionSchema
from ..models import Conversation

PROMPT_VERSION = 3

# Added when the source gave no speaker labels (e.g. a Zalo Web copy is bare
# message bodies). Vietnamese pronoun use is a strong turn-taking signal, so the
# model can recover roles from context far better than any regex.
UNLABELLED_SPEAKERS = """\
IMPORTANT: this text was copied out of the chat app, which preserved the lines but NOT who \
sent them. Before extracting, work out which of these two shapes you are looking at:

(a) A CONVERSATION between a customer and the shop, with the speaker labels stripped. \
Recover the turn-taking from context:
  - Vietnamese pronouns are the strongest signal. A customer typically self-refers as "em" \
and addresses the seller as "chị"/"anh"/"shop"; the seller often uses "m"/"mình"/"bên chị" \
and calls the customer "b"/"bạn"/"em".
  - Questions about price, stock, delivery and payment usually come from the customer; \
quotes, availability, shipping fees and bank details usually come from the seller.
  - Speakers normally alternate, but either side may send several lines in a row.

(b) A SINGLE STRUCTURED NOTE, such as an order written as one message: a header line with a \
date and order number, then item lines (quantity first), then a delivery address, a phone \
number, a total, a deposit, and any trailing note. Here there is no turn-taking at all — do \
not invent a dialogue. Read it as one record and map the lines to the fields you need.

Decide which it is, then extract. If a field depends on who said something and you genuinely \
cannot tell, return null rather than guessing."""

NO_TIMESTAMPS = """\
IMPORTANT: the source recorded NO message timestamps. Do not infer, estimate or invent when \
a message was sent -- any field asking for that must be null.

Dates and times written INSIDE the text are different: a header like "15/8" or a line like \
"12h30" is content the sender typed, and those ARE extractable when a field asks for them. \
A date given as day/month with no year means the current or most recent such date; do not \
guess a year that was not written."""

SYSTEM = """\
You extract structured data from customer-service chat conversations.

Rules, in priority order:
1. Only report information actually present in the conversation. Never infer, guess, or \
fill in what a typical conversation would contain.
2. If a field is not stated, return null for it. A null is a correct answer; a plausible \
invention is a serious error.
3. Quote values as the customer expressed them. Do not translate, normalize casing, or \
tidy phrasing unless the field description explicitly asks for it.
4. When the conversation contradicts itself, use the most recent statement.
5. Conversations may be in Vietnamese, English, or a mix. Read both. Return field values \
in the language the field description specifies; if it does not specify, keep the source \
language.

You must call the `record_extraction` tool exactly once with your result."""

USER_TEMPLATE = """\
<conversation>
<source>{source}</source>
<conversation_id>{conversation_id}</conversation_id>
<customer>{customer}</customer>
<period>{period}</period>
<message_count>{count}</message_count>

<transcript>
{transcript}
</transcript>
</conversation>
{instructions}
Extract every field defined in the tool schema. Use null for anything not stated."""


def build_user_prompt(
    conv: Conversation,
    schema: ExtractionSchema,
    *,
    max_chars: int,
    display_timezone: str = "UTC",
) -> str:
    tz = tz_zone(display_timezone)
    transcript = conv.transcript(tz=tz)

    if len(transcript) > max_chars:
        # Keep both ends: openings carry identity/intent, closings carry outcome.
        head = int(max_chars * 0.4)
        tail = max_chars - head
        transcript = (
            transcript[:head]
            + f"\n\n[... {len(transcript) - max_chars} characters of the middle omitted ...]\n\n"
            + transcript[-tail:]
        )

    period = "not recorded by the source"
    if conv.started_at and conv.last_message_at:
        period = (f"{conv.started_at.astimezone(tz):%Y-%m-%d} to "
                  f"{conv.last_message_at.astimezone(tz):%Y-%m-%d} ({display_timezone})")

    notes = []
    if not conv.speakers_known:
        notes.append(UNLABELLED_SPEAKERS)
    if not conv.timestamps_known:
        notes.append(NO_TIMESTAMPS)
    if schema.instructions:
        notes.append(schema.instructions)

    instructions = ("\n<additional_instructions>\n" + "\n\n".join(notes)
                    + "\n</additional_instructions>\n") if notes else ""

    return USER_TEMPLATE.format(
        source=conv.source.value,
        conversation_id=conv.conversation_id,
        customer=conv.customer_name or "unknown",
        period=period,
        count=len(conv.messages),
        transcript=transcript or "(no text messages)",
        instructions=instructions,
    )


# --------------------------------------------------------------------- segmentation

# Bumped independently of PROMPT_VERSION: this prompt drives a different call, with its
# own cache key, and a wording change here must not invalidate every field extraction.
SEGMENT_PROMPT_VERSION = 3

# Everything scripts/zalo_capture.py knows about the shape of this shop's messages, said
# in words instead of regular expressions.
#
# The regexes are not being replaced because they are wrong -- they are replaced because
# each one encodes a single phrasing, and every phrasing nobody anticipated was lost in
# silence: a header written "(Tên KH)" instead of "- Tên KH" dropped whole orders, a
# product called "gương cộc" truncated its order at the first line, and a revision worded
# outside a nine-phrase list vanished with no counter and no log. A model reads intent, so
# an unanticipated phrasing is a judgement call rather than a miss.
#
# SPLIT IN TWO ON PURPOSE. The chat reaches this program two ways -- pasted as text from a
# desktop, or filmed off a phone screen because a phone cannot select-all a conversation --
# and only the ANSWER differs between them. What an order looks like, which dates are
# transposed, that "gương cộc" is a mirror, what a trailing line might mean: that is one
# body of knowledge about one shop, and keeping two copies of it means fixing every future
# discovery twice and finding out later that one copy was missed.
#
# So ORDER_CONTEXT is the shop, and each contract below is only how to reply.
ORDER_CONTEXT = """\
The chat belongs to a Vietnamese bathroom-fittings shop. Staff post orders as single \
messages, and everyone else talks around them. Your job is to find every order and attach \
any later message that changes an order to the order it changes.

You do NOT convert money, compute totals, or decide which version of an order is correct.

## Report EVERY order

The chat may contain a hundred orders. Report all of them. A partial answer is a failure: \
an order you leave out is money missing from a spreadsheet, with nothing to show it was \
ever there.

## An order starts with a header line

A header carries a date, an order number, and usually a customer name. All of these are \
real headers from this chat:

    15/8 đơn 1 - Meloxicam
    2/7 đơn 2 (Trần Thị Liên)
    13/07 don 5  Chị Hương
    17/7 đơn 1: Thảo Nguyên
    29/6 đơn 4- Bùi Đức Hạnh
    5/8 đơn 3

Read the day, month, order number and customer name out of it. If a header states no \
customer name, return null for it -- never take a name from elsewhere in the message.

## Dates are day-first

Vietnamese convention is day/month, so "8/3" is the 8th of March.

This capture is for month {month} of {year}. When a header's month is not {month} but its \
DAY is, the two may have been typed the wrong way round -- but only treat it that way when \
the orders immediately before and after it are literally month {month}. A single order \
belonging to another month is normal; this chat routinely covers several, and you must \
report those too. Set date_swapped when you swap, and report day and month AFTER swapping.

## An order's body ends at its money

After the header come, in this order and each of them optional:
  - product lines, ALWAYS beginning with a quantity
  - a delivery address, sometimes prefixed "Đc:" or "Địa chỉ:"
  - a phone number, on its own line or inside the address
  - a total, usually after "Tổng"
  - a deposit, usually after "Đã cọc" or "Cọc"
  - a "Note:" line

CAREFUL: "gương cộc" is a PRODUCT -- a mirror -- not a deposit. A line naming a product \
that happens to contain the word "cọc" is a product line.

## Everything after the body is one of four things

For the lines between one order's end and the next header:

  body     -- still part of the order above: a late address, a second phone number
  update   -- a message CHANGING that order, carrying no header of its own:
                "Đơn này lấy thêm 1 cây sen / Thu thêm 2.900"
                "THAY ĐỔI - Lan Anh"
                "Tổng 12.000"
                "đã cọc thêm 1tr"
              Anything stating a different amount, an added or removed product, or a \
              corrected name or address for the order above.
  version  -- a SECOND FULL ORDER repeating a header already seen, with different \
              contents. Report it as its own order and set repeats_header.
  chatter  -- ordinary group conversation: "ok chị", "vâng ạ", "đơn này đi chưa ạ?", \
              greetings, @mentions, delivery questions stating no new figure. Leave it out.

When you cannot tell an update from chatter, report it as an update with confidence "low". \
These errors are not equal: a revision wrongly kept costs the reader one glance, while a \
revision wrongly dropped is money missing with nothing left to show it was ever there.

## Report money exactly as written

Never convert it. "29tr", "2tr5", "500k", "5.800", "13.800" -- copy the characters as \
they appear. A separate deterministic step converts them and already knows this shop's \
conventions; if you return a number instead, you will be wrong about "1tr8".

## Rules

1. Report only what is written. Never invent an order, a name, or a figure.
2. Never merge two orders and never split one. The header count is the order count.
3. Copy text verbatim: no tidying, no translating, no normalising case.
4. An order stating no total is still an order."""


# --- how to answer, when the chat arrived as pasted text ---------------------
#
# Line numbers, never text. Version 1 asked for each order's body verbatim, which was wrong
# twice over: it made the answer as long as the question -- a real month is ~12k tokens, so
# a complete reply could not fit in any sane output budget, and the first live run returned
# 1 order out of 69 -- and it put the order's own words through a model that could
# paraphrase them. Line numbers are ~15 tokens per order instead of ~180, and the text is
# sliced out of the paste by this program, so "verbatim" is arithmetic rather than a
# request the model may decline.
TEXT_CONTRACT = """\

## How to answer

Every line of the chat is given to you with a number. You answer ONLY in line numbers. \
Never copy the text back -- this program slices it out itself.

Give `header_line` for an order's header and `end_line` for its last line. Report a \
revision in `updates` as a start_line and an end_line. Work through the chat from the \
first line to the last.

If the chat begins part-way through an order, with no header above the first lines, \
ignore those lines."""


# --- how to answer, when the chat arrived as frames off a phone screen --------
#
# Here the model must transcribe, because there are no line numbers in a photograph. That
# reintroduces exactly the paraphrase risk the text contract exists to remove, which is why
# every order read this way is flagged for a human to check against the total. The prompt
# leans hard on copying rather than summarising for the same reason.
#
# Overlap between frames is deliberately NOT the model's problem. The same order filmed
# three times arrives as three orders with one key, and the capture path has deduplicated
# on (day, month, số đơn) since long before video existed; an order clipped by a frame edge
# is replaced by the fuller copy from the next frame by merge_into, also already there.
# Asking the model to reconcile frames would be asking it to redo, less reliably, work that
# is already exact.
VIDEO_CONTRACT = """\

## How to answer

These images are consecutive frames of a phone screen, filmed while scrolling the chat \
upward through time. Frame 1 is the earliest.

Read every order you can see, frame by frame, and TRANSCRIBE its text exactly as it \
appears -- character for character, including diacritics, punctuation and the way numbers \
are written. Do not summarise, correct spelling, or tidy anything. If you are unsure of a \
character, copy your best reading rather than leaving it out.

Say which frame each order came from.

Consecutive frames overlap, so the same order appears in several of them. Report it each \
time you see it -- do not try to remove the repeats, and do not merge frames together. \
This program removes them exactly, by the order's own date and number.

If an order is cut off by the top or bottom edge of a frame, report what you can see and \
set `partial`. It will be whole in a neighbouring frame.

Money is the part most easily got wrong and the part that matters most. Read "5.800" and \
"5.800.000" and "5tr8" as the different things they are, and copy the dots exactly."""


SEGMENT_USER_TEMPLATE = """\
Segment the chat below into orders. The capture is for month {month}/{year}, but report \
orders from every month you find.

Each line is numbered. Answer in those numbers.

<chat>
{text}
</chat>

The chat has {count} lines. Report every order in it."""


VIDEO_USER_TEMPLATE = """\
These are {count} frames of a Zalo group chat, filmed on a phone in order. The capture is \
for month {month}/{year}, but report orders from every month you find.

Read every order visible in every frame."""


def number_lines(text: str) -> tuple[str, list[str]]:
    """(numbered text for the prompt, the original lines).

    1-based, because the model is told "line 1" and an off-by-one here would shift every
    order's body by a line -- silently, since a body missing its first product still looks
    like a body.
    """
    lines = text.splitlines()
    numbered = "\n".join(f"{i}|{line}" for i, line in enumerate(lines, start=1))
    return numbered, lines


def build_segment_prompt(text: str, month: int, year: int) -> tuple[str, str, list[str]]:
    """(system, user, source lines) for one segmentation call over pasted text."""
    numbered, lines = number_lines(text)
    system = ORDER_CONTEXT.format(month=month, year=year) + TEXT_CONTRACT
    return (system,
            SEGMENT_USER_TEMPLATE.format(month=month, year=year,
                                         text=numbered, count=len(lines)),
            lines)


def build_video_prompt(frame_count: int, month: int, year: int) -> tuple[str, str]:
    """(system, user) for one segmentation call over screen-recording frames.

    Same shop knowledge as the text path, by construction rather than by copy.
    """
    system = ORDER_CONTEXT.format(month=month, year=year) + VIDEO_CONTRACT
    return (system,
            VIDEO_USER_TEMPLATE.format(count=frame_count, month=month, year=year))
