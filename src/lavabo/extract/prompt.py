"""Prompt construction. Shared by every provider so results stay comparable.

Bump PROMPT_VERSION on any wording change: it is part of the extraction cache key, so
changing the prompt correctly invalidates previously cached results.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from ..config import ExtractionSchema
from ..models import Conversation

PROMPT_VERSION = 2

# Added when the source gave no speaker labels (e.g. a Zalo Web copy is bare
# message bodies). Vietnamese pronoun use is a strong turn-taking signal, so the
# model can recover roles from context far better than any regex.
UNLABELLED_SPEAKERS = """\
IMPORTANT: this transcript has NO speaker labels. Each line is one message, in order, but \
the app did not record who sent it. Work out the turn-taking from context before extracting:

- Vietnamese pronouns are the strongest signal. A customer typically self-refers as "em" and \
addresses the seller as "chị"/"anh"/"shop"; the seller often uses "m"/"mình"/"bên chị" and \
calls the customer "b"/"bạn"/"em".
- Questions about price, stock, delivery and payment usually come from the customer; quotes, \
availability, shipping fees and bank details usually come from the seller.
- Speakers normally alternate, but either side may send several lines in a row.

Attribute every line before you extract. If a field depends on who said something and you \
genuinely cannot tell, return null rather than guessing."""

NO_TIMESTAMPS = """\
IMPORTANT: this transcript has NO timestamps -- the source did not provide them. Do not \
infer, estimate or invent any date or time. Times mentioned inside the message text (like \
"12h30" or "tầm chiều 2h") are things the participants said, not message timestamps; use \
them only if a field explicitly asks about what was discussed. Any field asking when a \
message was sent must be null."""

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
    tz = ZoneInfo(display_timezone)
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
