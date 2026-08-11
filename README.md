# lavabo-extract-agent

Local ETL agent: customer conversations from **Zalo** and **Meta business chat** →
structured **Excel**, with an LLM doing the extraction step.

```
Zalo  ──drop──▶ parse ─┐
                       ├─▶ canonical ─▶ SQLite ─▶ LLM extract ─▶ Excel
Meta  ──poll──▶ Graph ─┘
```

---

## Start here

| Document | What's in it |
|---|---|
| **[docs/01-source-verification.md](docs/01-source-verification.md)** | **Task 1** — what Zalo and Meta can actually give us, with confidence levels. Read this first: it contains one finding that changes the plan. |
| [docs/02-agent-plan.md](docs/02-agent-plan.md) | **Task 2** — the architecture, stage by stage, and the build order |
| [docs/03-zalo-runbook.md](docs/03-zalo-runbook.md) | The human SOP for getting Zalo conversations in |
| [docs/04-meta-setup.md](docs/04-meta-setup.md) | Meta app, tokens, App Review, troubleshooting |
| [docs/05-schema-guide.md](docs/05-schema-guide.md) | How to define your columns when the requirements land |

**The headline from Task 1:** Zalo PC's "Export data" is an *encrypted, restore-only backup*,
not a data export — it can't be parsed, by a script or by an AI, because the blocker is
encryption rather than comprehension. Zalo therefore runs as a drop-a-file source
until/unless you get an Official Account. Meta's Graph API path works as you expected, gated
on Advanced Access.

Verify both Zalo findings yourself — neither probe prints message content:

```bash
python scripts/probe_zalo_export.py  "C:/Users/<you>/Desktop/backup_zalo_....zip"   # the backup
python scripts/probe_zalo_appdata.py                                               # the app's local storage
```

The manual capture is nonetheless mostly automated — `scripts/zalo_capture.py` watches the
clipboard, derives the customer name from the transcript, and writes the files, so each
conversation costs a Ctrl+A/Ctrl+C rather than a trip through Notepad.

---

## Install

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .

cp .env.example .env                                   # add your API keys
cp config/config.example.yaml config/config.yaml
cp config/schema.example.yaml config/schema.yaml       # then edit the columns
```

## Use

```bash
lavabo check                              # preflight: credentials, paths, schema
lavabo ingest --source meta               # pull from Graph API (incremental)
lavabo ingest --source zalo               # parse files in data/inbox/zalo/
lavabo extract --limit 5 --dry-run        # see the prompt + token estimate, spend nothing
lavabo extract                            # run the LLM step (cached)
lavabo load --out data/out/report.xlsx    # write the workbook
lavabo verify                             # sanity checks, non-zero exit on failure

lavabo run --out data/out/report.xlsx     # all of the above
```

Every stage is independently re-runnable. Extraction results are cached on
`content + schema_version + prompt_version + model`, so re-running costs nothing unless
something actually changed.

## Output

Three sheets:

- **Data** — one row per conversation, columns exactly as `config/schema.yaml` orders them.
  Amber cells are values the model declined to guess.
- **Sources** — IDs, message counts, date ranges, and a link back to the original thread, so
  any disputed cell is checkable in 30 seconds.
- **Run** — model, schema version, counts, fill rate, token spend.

## Current status

| | |
|---|---|
| Findings + plan + probe | delivered |
| Meta connector, staging, extraction, Excel | written, **not yet run against live credentials** |
| Zalo transcript parser | written; the line regex needs tuning against one real sample |
| Your column requirements | **pending** — everything else is ready for them |

## Layout

```
docs/          findings, plan, runbooks
config/        config.yaml + schema.yaml (your columns)
scripts/       probe_zalo_export.py, probe_zalo_appdata.py, zalo_capture.py
src/lavabo/    models, config, store, cli
  connectors/  meta_graph.py, zalo_export.py
  extract/     anthropic, gemini (same interface, swap in config)
  load/        excel.py
data/          inbox/, staging.db, out/     [gitignored — never commit chat content]
```

## Notes

- **Customer conversation content must never be committed.** `data/` is gitignored.
- Conversation text is sent to whichever LLM provider you configure, and nowhere else.
- Secrets live in `.env` only.
