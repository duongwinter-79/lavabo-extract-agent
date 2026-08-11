# Zalo runbook (human SOP)

Zalo PC's "Export data" is an encrypted, restore-only backup, and there is no
per-conversation export — see `docs/01-source-verification.md`. For a personal (non-OA)
account the manual transcript below is the only zero-risk path. It takes about two minutes
per conversation.

---

## Step 0 (once) — prove it for yourself

Before accepting the manual route, run the export and probe it. If Zalo has changed and the
archive is readable, tell me and the connector gets rewritten to parse it directly.

```
Zalo PC → gear icon → Data / Storage (Quản lý dữ liệu) → Backup / Export data
```

```bash
python scripts/probe_zalo_export.py "C:/Users/<you>/Desktop/backup_zalo_20260811.zip" -o probe.md
```

The probe prints structure and format verdicts only — no message content — so `probe.md` is
safe to share.

---

## Step 1 — capture a conversation

1. Open the conversation in Zalo PC.
2. Scroll to the very top of the range you want. **Zalo lazy-loads history** — if you don't
   scroll up first, you will only copy the last screenful.
3. Click the first message, scroll to the bottom, `Shift+Click` the last message.
   (Or `Ctrl+A` inside the message pane, depending on version.)
4. `Ctrl+C`.
5. Paste into Notepad — **plain text, not Word.** Word introduces smart quotes and
   invisible formatting that break the parser.
6. Save as UTF-8 into `data/inbox/zalo/`.

## Step 2 — name the file correctly

**The filename becomes the customer name in Excel.** Use the customer's name, nothing else:

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

## Tuning the line pattern (one-time, ~10 minutes)

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
