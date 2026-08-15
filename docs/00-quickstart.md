# Quickstart — first run with ONE Zalo conversation

Goal: get one real conversation from Zalo into an Excel file, end to end, so you know the
pipeline works before spending 45 minutes capturing 50 of them.

**No Meta setup needed.** Meta is disabled by default; this whole guide is Zalo-only.

Every command below was run against a clean clone before being written down.

---

## 0. Prerequisites

- **Python 3.11 or newer**. 3.10 and below will not work.
- **git**
- **Zalo desktop or Zalo Web**, logged in
- An **Anthropic or Gemini API key** — only needed at step 8. Steps 1–7 work without one.

### macOS: check your Python first

macOS ships Python **3.9**, which is too old, and `python` (without the 3) usually doesn't
exist at all. Check:

```bash
python3 --version
```

If it says 3.9 or 3.10, install a newer one:

```bash
brew install python@3.12
python3.12 --version
```

Then use `python3.12` wherever this guide says `python3`. This is the single most common
macOS stumbling block — `pip install -e .` fails with a `requires-python` error otherwise.

---

## 1. Clone

```bash
git clone https://github.com/duongwinter-79/lavabo-extract-agent.git
cd lavabo-extract-agent
```

## 2. Install

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
```

**macOS / Linux:**
```bash
python3 -m venv .venv          # or python3.12 -m venv .venv, see step 0
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

On macOS the clipboard works out of the box — `pyperclip` uses the built-in `pbpaste`, and
reading the clipboard needs no Accessibility or Privacy permission.

Check it worked:
```bash
lavabo --help
```

> `lavabo: command not found` → the venv isn't active. Re-run the activate line. On Windows
> you'll see `(.venv)` at the start of your prompt when it is.

## 3. Create your config files

```bash
cp config/config.example.yaml config/config.yaml
cp config/schema.example.yaml config/schema.yaml
cp .env.example .env
```

Windows PowerShell uses `copy` instead of `cp`; on macOS/Linux `cp` is correct.

> **`config/config.yaml` is yours and gitignored.** `git pull` updates
> `config.example.yaml` but never your copy — so when the example changes (a new default
> provider or model, say), you have to copy the change across yourself. `lavabo check`
> reports mismatches such as a Gemini provider left pointing at a Claude model.

### Edit `config/config.yaml` — one field matters right now

```yaml
zalo:
  own_names:
    - "Lavabo Store"        # <- YOUR exact Zalo display name
```

**Get this exactly right.** It is how the agent tells your messages from the customer's.
Wrong or empty means every message is labelled as coming from the customer, and nothing
errors — it just quietly produces wrong data.

Leave everything else alone for now. `schema.yaml` keeps its placeholder columns until your
real requirements are ready.

## 4. Preflight

```bash
lavabo check
```

Expected:

```
staging db: data/staging.db
{ "conversations": 0, "messages": 0, ... }
schema:     v1, 8 columns (customer_name, phone_number, intent, ...)
llm:        anthropic / claude-opus-5 — ANTHROPIC_API_KEY NOT set (fine until you run `lavabo extract`)
OK   zalo: 0 file(s) in data/inbox/zalo
```

Exit code 0. The missing API key is expected at this stage.

> `zalo: ... WARNING: zalo.own_names is empty` → go back to step 3.

## 5. Capture ONE conversation

Leave this running in its own terminal:

```bash
python scripts/zalo_capture.py
```

Then, in Zalo — desktop or web, **pick one client and stick with it**:

1. Open a conversation (start with a **short** one for this test)
2. Scroll to the top of it — Zalo lazy-loads history, so without this you only get the last screenful
3. Select all and copy:
   - **macOS:** `Cmd+A`, then `Cmd+C`
   - **Windows:** `Ctrl+A`, then `Ctrl+C`

The terminal should print:

```
  saved Nguyễn Văn An.txt  (47 messages, 3,201 chars)
```

Then stop the script with **`Ctrl+C` in the terminal** — that's `Ctrl`, not `Cmd`, even on
macOS. Terminal interrupt is always `Ctrl+C`; only the *copy* in Zalo uses `Cmd`.

### If nothing was saved

The script only accepts text it recognises as a transcript. Nothing printed means the copied
format doesn't match the built-in patterns — **expected, and easy to fix.** Save the copy
manually so we have a sample, as **UTF-8 plain text** at
`data/inbox/zalo/<customer name>.txt`:

- **macOS — TextEdit defaults to RTF, which breaks parsing.** Open TextEdit, paste, then
  **Format → Make Plain Text** (`Shift+Cmd+T`) before saving. Or skip the GUI entirely:
  ```bash
  pbpaste > "data/inbox/zalo/Nguyễn Văn An.txt"
  ```
  That one-liner is the most reliable option on a Mac — it writes exactly what's on the
  clipboard, UTF-8, no formatting.
- **Windows:** paste into **Notepad** (not Word) and save as UTF-8.

Then send me the first 5 lines and I'll tune the pattern. Do not capture the other 49 until
this works.

## 6. Ingest

```bash
lavabo ingest --source zalo
```

Expected:

```
INFO  lavabo.connectors.zalo_export: Nguyễn Văn An.txt: pattern matched 47/47 lines (100%)
ingested 1 conversation(s), 47 new message(s)
```

**Two possible outcomes, both fine:**

- `pattern matched 47/47 lines (100%)` — your client includes sender names and timestamps.
- `no sender/timestamp structure — read 21 line(s) as messages` — your client (Zalo Web
  does this) copies only message bodies. The connector handles it: each line becomes a
  message and the model infers who said what at extraction time. **No timestamps are
  available from this source** — see docs/03-zalo-runbook.md.

Also sanity-check the message count against what you see in Zalo. If Zalo shows 200 messages
and this says 47, you didn't scroll far enough at step 5.

## 7. Look at the prompt before spending anything

```bash
lavabo extract --limit 1 --dry-run
```

Prints the exact prompt and a token estimate. **No API call, no cost, no key needed.** Read
the transcript in the output — the timestamps and who-said-what should look right.

## 8. Add your API key and extract

**Gemini's free tier is enough for this and needs no card.** Get a key at
<https://aistudio.google.com/apikey> and put it in `.env`:

```
GEMINI_API_KEY=AIza...
```

That matches the default in `config.example.yaml` (`provider: gemini`,
`model: gemini-3.1-flash`, `concurrency: 1`). Keep concurrency at 1 — the free tier allows
only a few requests per minute; 429s are retried with backoff, but going slow is faster than
being throttled.

For Anthropic instead, set `ANTHROPIC_API_KEY` and switch `provider`/`model` in
`config/config.yaml`.

Confirm the key actually works, and that the configured model exists:

```bash
lavabo check
lavabo models      # lists the models this key can use, flags the configured one
```

Model names change often. `lavabo models` asks the provider, so it is the authority
rather than whatever is written in the config example.

`OK llm: … key verified with the provider` means it is genuinely valid — `check` calls the
provider, so a wrong or truncated key is caught here rather than at the first extraction.

Then:

```bash
lavabo extract --limit 1
```

Expected:

```
  ok   zalo:Nguyễn Văn An:a1b2c3d4 (7/8 fields)
extracted 1, cached 0, failed 0
```

`7/8` means the model filled 7 columns and returned null for one — that's normal and correct
behaviour when something genuinely isn't stated in the conversation.

## 9. Write the Excel file

```bash
lavabo load --out data/out/first.xlsx
```

Open it. Three sheets:

- **Data** — your one row. **Amber cells are nulls** — the model declined to guess.
- **Sources** — message count, date range, source filename, so you can audit any cell.
- **Run** — model, schema version, fill rate, token spend.

## 10. Verify

```bash
lavabo verify
```

Passes, or tells you exactly which required column came back null too often.

---

## You're done — now scale up

Re-run step 5 for the other 49 conversations (the script keeps running; just keep copying),
then:

```bash
lavabo ingest --source zalo
lavabo extract
lavabo load --out data/out/report.xlsx
lavabo verify
```

Already-captured files and already-extracted conversations are skipped automatically, so
re-running is cheap and safe.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `lavabo: command not found` | venv not active | re-run the activate line |
| `python: command not found` (macOS) | macOS has no bare `python` | use `python3` |
| `requires-python` error on install | Python 3.10 or older | `brew install python@3.12`, see step 0 |
| `pip install` hits a system-python permission error | venv not active | activate first, never `sudo pip` |
| `ModuleNotFoundError: lavabo` | `pip install -e .` skipped | run it |
| `schema: NOT READY` | `config/schema.yaml` missing | step 3 |
| `ANTHROPIC_API_KEY is not set` | no key in `.env` | step 8 (or use `--dry-run`) |
| Capture script prints nothing | format not recognised | save manually, send me a sample |
| `No clipboard access` | missing backend | `pip install pyperclip` |
| Match rate below 50% | regex mismatch | send me the first 5 lines |
| Message count too low | didn't scroll to top | redo step 5.2 |
| Everything labelled inbound | `own_names` wrong | step 3 |
| Vietnamese shows as `Ã¡Â»` | saved as ANSI/RTF | re-save UTF-8; on macOS use `pbpaste > file.txt` |
| macOS: file full of `{\rtf1...}` | TextEdit saved RTF | Format -> Make Plain Text, or use `pbpaste` |

## What to send me if you get stuck

1. The command you ran and its full output
2. The first 5 lines of the `.txt` (scrub names if you like — I need the *shape*, not content)
3. `lavabo check` output

That's enough to fix a pattern issue in about ten minutes.
