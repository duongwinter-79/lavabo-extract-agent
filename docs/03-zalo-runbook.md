# Zalo runbook (human SOP)

Zalo PC's "Export data" is an encrypted, restore-only backup, and there is no
per-conversation export — see `docs/01-source-verification.md`. For a personal (non-OA)
account the manual transcript below is the only zero-risk path. It takes about two minutes
per conversation.

---

## Step 0 (once) — prove it for yourself

Before accepting the manual route, rule out the two automated options. Both probes print
structure and format verdicts only — never message content — so the reports are safe to share.

**a) The backup archive.** Run the export, then probe it:

```
Zalo PC → gear icon → Data / Storage (Quản lý dữ liệu) → Backup / Export data
```

```bash
python scripts/probe_zalo_export.py "C:/Users/<you>/Desktop/backup_zalo_20260811.zip" -o probe.md
```

**b) The app's local storage.** Zalo PC is Electron-based, so it may cache recent messages
on disk in a readable store even though the backup is encrypted. Separate question, separate
probe:

```bash
python scripts/probe_zalo_appdata.py -o probe-appdata.md
```

On macOS this also searches `~/Library/Containers/` — Zalo for Mac ships through the App
Store, so it is sandboxed and its data does not sit in the usual Application Support path.

If either comes back readable, send me the report — the connector gets rewritten to read it
directly and everything below disappears.

---

## Step 1 — capture conversations (fast path)

`scripts/zalo_capture.py` watches your clipboard and writes the files for you. No Notepad,
no Save As, no naming. **The customer name is derived from the transcript itself** — the
script parses sender names, discards the ones in `zalo.own_names`, and uses the most
frequent remaining name.

```bash
python scripts/zalo_capture.py
```

Then per conversation: **click it → scroll to the top → select all → copy**
(`Cmd+A`/`Cmd+C` on macOS, `Ctrl+A`/`Ctrl+C` on Windows). The script detects the copy, names
the file, skips duplicates, and prints a confirmation. Stop it with `Ctrl+C` in the terminal
— always `Ctrl`, even on macOS. Roughly 10 seconds per conversation instead of two minutes.

If it can't tell who the customer is (e.g. every sender matched `own_names`) it asks you to
type the name rather than guessing. `--name "Tran Thi B"` forces it for one capture.

Requires `pyperclip` (`pip install pyperclip`); falls back to stdlib `tkinter` if absent.

### Manual fallback

If the clipboard watcher misbehaves, do it by hand — the ingest step is identical:

1. Open the conversation, scroll to the very top of the range you want.
   **Zalo lazy-loads history** — without scrolling first you only copy the last screenful.
2. Click the first message, `Shift+Click` the last (or `Ctrl+A` in the message pane), `Ctrl+C`.
3. Save as **UTF-8 plain text** into `data/inbox/zalo/`, named after the customer.
   - macOS: `pbpaste > "data/inbox/zalo/<name>.txt"` is the most reliable route. TextEdit
     saves RTF by default — if you use it, Format -> Make Plain Text first.
   - Windows: paste into Notepad, not Word (Word adds smart quotes that break parsing).

## Step 2 — file naming (manual path only)

`zalo_capture.py` handles this for you. If you saved files by hand,
**the filename becomes the customer name in Excel** — use the customer's name, nothing else:

```
data/inbox/zalo/Nguyen Van An.txt
data/inbox/zalo/Tran Thi Bich - 0901234567.txt
```

Avoid `chat1.txt`, `export (3).txt`, or dates in the name.

## Step 3 — ingest

```bash
lavabo ingest --source zalo
```

Files are hashed, so re-running is safe and already-ingested files are skipped. Editing a
file changes its hash and it will be re-ingested as a new conversation — so fix the text
*before* the first ingest, or use `--full` and clear the old row.

---

## Order notes (the main case)

Many captures are not conversations at all but a single message holding a whole order:

```
15/8 đơn 1 - Meloxicam
1 tủ BC52, gương bo, mặt tinh thể - 80- 401
2 sen cơ như hình
<địa chỉ giao hàng>
<số điện thoại>
Tổng 29tr
Đã cọc 500k
Note: ...
```

**The header line carries three facts and is parsed, not guessed:** date, order number,
and the customer's Zalo display name. Both shapes are recognised:

| Header | Parsed as |
|---|---|
| `15/8 đơn 1 - Meloxicam` | 15/8, order 1, customer "Meloxicam" |
| `15/8 - đơn 4` | 15/8, order 4, no customer |

They appear in the workbook's **Sources** sheet as `customer_name`, `order_date` and
`order_no`. Because a regex settles them exactly, these should stay **derived columns** when
your real requirements arrive — asking the model for them would swap certainty for
probability at no benefit. Everything needing judgement (items, quantities, totals,
deposit, notes) is what the model is for.

A year is only recorded when written. `15/8` stays `15/8` — no year is invented.

---

## What a Zalo Web copy actually contains (confirmed)

Verified against a real conversation: **selecting and copying in Zalo Web yields only the
message bodies — one per line, with no sender names and no timestamps.**

```
Em chào shop ạ
Em muốn đặt mẫu này còn hàng không ạ
Mẫu đó bên m hết rồi bạn nhé
Vậy còn mẫu kia không chị
```

*(illustrative example, not a real conversation)*

There is no structure for a regex to parse, so the connector detects this and switches to
**plain mode**: each line becomes one message, in order, with `sent_at = null` and
`direction = unknown`. Nothing is fabricated.

Speaker attribution then happens at extraction time, where the model reads the whole
conversation and works out turn-taking from context. Vietnamese pronouns make this reliable
— a customer says "em … chị", the seller says "m/mình/bên chị … b/bạn" — and the prompt
tells the model exactly that.

### The real cost: no timestamps, ever

| | Available from a Zalo Web copy? |
|---|---|
| Message text | yes |
| Order of messages | yes |
| Who said what | inferred at extraction, not recorded |
| **Date / time of any message** | **no — permanently unavailable** |
| Customer name | only from the filename you choose |

**Any column that needs a date or time cannot be filled from this source.** The Excel
`first_message` / `last_message` cells stay blank rather than showing an invented date.
Times *mentioned inside* messages ("12h30", "tầm chiều 2h") are still extractable, because
those are things the participants said.

Two consequences worth acting on:

1. **Name the file after the customer** — with no names in the text, the filename is the
   only record of who the conversation is with.
2. **Try Zalo Desktop before capturing all 50.** If its copy includes names or timestamps,
   it is strictly better data and the connector already handles that format. One capture
   tells you: `pbpaste | head -20`.

---

## Tuning the line pattern (one-time, ~10 minutes)

Only relevant if your client *does* produce `Name (time): text` lines — Zalo Web does not,
and falls to plain mode automatically.

The parser doesn't know Zalo's exact copy-paste format yet, so it tries several candidates
and picks whichever matches the most lines. It logs the match rate:

```
INFO lavabo.connectors.zalo_export: Nguyen Van An.txt: pattern matched 84/91 lines (92%)
```

- **Above 90%** — good, nothing to do.
- **Below 50%** — you get a warning. Fix it:

1. Open your `.txt` and look at one message line, e.g.

   ```
   Nguyễn Văn An (14:32 25/12/2025): Cho em hỏi giá lavabo treo tường
   ```

2. Write a regex with three named groups — `name`, `ts`, `text`:

   ```yaml
   zalo:
     line_patterns:
       - '^(?P<name>[^(]{1,60})\s*\((?P<ts>[\d/:\s]{8,25})\)\s*:\s*(?P<text>.*)$'
   ```

3. Re-run `lavabo ingest --source zalo --full` and check the match rate again.

If your timestamp format isn't in `TIMESTAMP_FORMATS` (`src/lavabo/connectors/zalo_export.py`),
add it there — it's a one-line change.

**Send me one real sample file (a short one, or with names scrubbed) and I'll do this for
you and add a test.**

---

## Gotchas

| Symptom | Cause | Fix |
|---|---|---|
| Every message shows as inbound | `zalo.own_names` empty or misspelt | Set it to your exact Zalo display name |
| `zalo_capture.py` asks for every name | Same cause — it can't tell you from the customer | Same fix |
| `zalo_capture.py` ignores your copy | Text didn't match any sender pattern | Tune `line_patterns` below, or use `--name` |
| "No clipboard access" | Missing clipboard backend | `pip install pyperclip` |
| Only ~20 messages captured | Didn't scroll up before selecting | Redo step 1.2 |
| Garbled Vietnamese diacritics | Saved as ANSI | Re-save as UTF-8 |
| Timestamps an hour off | Wrong `zalo.timezone` | Set `Asia/Ho_Chi_Minh` |
| Messages merged together | Multi-line bodies attach to the previous message — this is intended | — |
| Same conversation twice | File was edited after ingest (hash changed) | Delete the old row, re-ingest with `--full` |

---

## If you get a Zalo OA

This whole document disappears. `/v2.0/oa/listrecentchat` and `/v2.0/oa/conversation` give
scheduled, automatic pulls exactly like Meta. Building that connector is roughly a day's
work and it drops in behind the same `Connector` interface — nothing downstream changes.

**This is the single highest-leverage decision on the project. Worth checking whether your
business qualifies.**
